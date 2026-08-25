"""The verified event flow: challenge echo, dedupe, correlation matching, the
answer forward, and the door-status policy. Every request here is signed with
the test secret — the flow always crosses real verification first."""

from __future__ import annotations

import json
from typing import Any

import pytest
from tai42_contract.channels import AnswerForwardError, InboundAnswerOutcome
from tai42_kit.settings import reset_all_settings

from tai42_channel_slack.inbound import slack_inbound

from .conftest import (
    TEST_ALLOWED_RECIPIENT,
    TEST_DEFAULT_RECIPIENT,
    TEST_SIGNING_SECRET,
    body_json,
    make_request,
    signed_headers,
)

pytestmark = pytest.mark.usefixtures("slack_env")

_CALLBACK = "http://gateway/api/interactions/callback/ticket-7"
_THREAD_TS = "1712345678.000100"
_CORR_KEY = f"channel:slack:corr:{_THREAD_TS}"
_DEDUPE_KEY = "channel:slack:event:Ev001"


def _signed(body: bytes):
    return make_request(body, signed_headers(body, TEST_SIGNING_SECRET))


def _event_body(event: dict[str, Any] | None = None, event_id: str | None = "Ev001", **envelope: Any) -> bytes:
    payload: dict[str, Any] = {"type": "event_callback", **envelope}
    if event_id is not None:
        payload["event_id"] = event_id
    if event is not None:
        payload["event"] = event
    return json.dumps(payload).encode()


def _reply_event(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "message",
        "channel": TEST_DEFAULT_RECIPIENT,
        "thread_ts": _THREAD_TS,
        "text": "yes, deploy it",
        "user": "U012345",
        "ts": "1712345679.000200",
    }
    event.update(overrides)
    return event


def _seed_correlation(fake_redis) -> None:
    # The corr record is now a JSON {callback_url, interaction_id, timeout_at}.
    fake_redis.store[_CORR_KEY] = json.dumps(
        {"callback_url": _CALLBACK, "interaction_id": "int-7", "timeout_at": "2999-01-01T00:00:00+00:00"}
    )
    fake_redis.ttls[_CORR_KEY] = 300


async def test_url_verification_echoes_challenge():
    body = json.dumps({"type": "url_verification", "challenge": "ch4ll3ng3"}).encode()

    response = await slack_inbound(_signed(body))

    assert response.status_code == 200
    assert body_json(response) == {"challenge": "ch4ll3ng3"}


async def test_unsigned_url_verification_is_401():
    # Verification precedes the challenge echo — an unverified echo would let
    # anyone confirm the endpoint.
    body = json.dumps({"type": "url_verification", "challenge": "ch4ll3ng3"}).encode()

    response = await slack_inbound(make_request(body, {}))

    assert response.status_code == 401


async def test_url_verification_without_challenge_is_400():
    body = json.dumps({"type": "url_verification"}).encode()

    response = await slack_inbound(_signed(body))

    assert response.status_code == 400


@pytest.mark.parametrize("body", [b"{not json", b"[1, 2]", b'"a string"'])
async def test_non_object_body_is_400(body):
    response = await slack_inbound(_signed(body))

    assert response.status_code == 400
    assert body_json(response) == {"error": "body must be a JSON object"}


async def test_event_callback_without_event_id_is_400():
    response = await slack_inbound(_signed(_event_body(event=_reply_event(), event_id=None)))

    assert response.status_code == 400
    assert body_json(response) == {"error": "event_callback without event_id"}


async def test_non_event_callback_envelope_is_ignored():
    body = json.dumps({"type": "app_rate_limited"}).encode()

    response = await slack_inbound(_signed(body))

    assert response.status_code == 200
    assert body_json(response) == {"status": "ignored"}


async def test_happy_path_forwards_answer_and_drops_correlation(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    response = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert response.status_code == 200
    assert body_json(response) == {"status": "forwarded"}
    (call,) = channels.inbound_calls
    assert call.correlation_key == _THREAD_TS
    assert call.answer == "yes, deploy it"
    assert call.bridge.owns_retry_notice is False  # a threaded reply is re-answerable in place
    assert _CORR_KEY not in fake_redis.store  # released by the ladder (mirrored)
    assert _DEDUPE_KEY in fake_redis.store  # claim kept: retries ack as duplicates


async def test_reply_from_allowlisted_conversation_forwards(fake_redis, channels):
    # A question can be delivered to any allowlisted recipient, so a
    # correlated reply from that conversation — not only the default one — is
    # a real answer that reaches the ladder.
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    event = _reply_event(channel=TEST_ALLOWED_RECIPIENT)
    response = await slack_inbound(_signed(_event_body(event=event)))

    assert body_json(response) == {"status": "forwarded"}
    (call,) = channels.inbound_calls
    assert call.answer == "yes, deploy it"


async def test_bridge_only_deployment_needs_no_ask_user_recipients(fake_redis, stub_conversations, monkeypatch):
    # No default recipient and an empty allowlist: ask_user correlation can never
    # match, but a bridge-only deployment must still forward messages to the bridge.
    monkeypatch.delenv("CHANNEL_SLACK_DEFAULT_RECIPIENT")
    monkeypatch.delenv("CHANNEL_SLACK_ALLOWED_RECIPIENTS")
    reset_all_settings()

    response = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.client_address == TEST_DEFAULT_RECIPIENT
    assert _DEDUPE_KEY in fake_redis.store  # processed: retries ack as duplicate


async def test_duplicate_event_id_acks_without_second_forward(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    first = await slack_inbound(_signed(_event_body(event=_reply_event())))
    second = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert body_json(first) == {"status": "forwarded"}
    assert body_json(second) == {"status": "duplicate"}
    assert len(channels.inbound_calls) == 1  # the ladder was consulted exactly once


async def test_ladder_bridged_outcome_acks_and_drops_correlation(fake_redis, channels):
    # The ladder's BRIDGED outcome (the ask is gone — it released the correlation and
    # already bridged the reply internally, under the event id dedupe key) acks 200. The
    # channel supplies the faithful bridge text and the event id.
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    response = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert body_json(response) == {"status": "bridged"}
    assert _CORR_KEY not in fake_redis.store  # released by the ladder (mirrored)
    assert _DEDUPE_KEY in fake_redis.store  # Slack's retry acks as duplicate
    (call,) = channels.inbound_calls
    assert call.bridge.bridge_text == "yes, deploy it"
    assert call.bridge.client_address == TEST_DEFAULT_RECIPIENT
    assert call.bridge.provider_message_id == "Ev001"


async def test_bridged_then_redelivery_acks_duplicate_no_second_ladder_call(fake_redis, channels):
    # After a BRIDGED outcome, Slack redelivers the same event_id: the dedupe claim
    # short-circuits — the retry acks as a duplicate and never re-runs the ladder.
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    first = await slack_inbound(_signed(_event_body(event=_reply_event())))
    assert body_json(first) == {"status": "bridged"}

    second = await slack_inbound(_signed(_event_body(event=_reply_event())))
    assert body_json(second) == {"status": "duplicate"}
    assert len(channels.inbound_calls) == 1  # still once


async def test_ladder_forward_error_does_not_bridge_and_keeps_correlation(fake_redis, channels, stub_conversations):
    # A raised AnswerForwardError (5xx/transport) must NOT be converted into a bridge:
    # the ladder kept the correlation and the channel re-raises for Slack's retry.
    _seed_correlation(fake_redis)
    channels.inbound_error = AnswerForwardError("callback forward failed: HTTP 500 from the interactions door")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert stub_conversations.accept_calls == []  # never bridged
    assert _CORR_KEY in fake_redis.store  # correlation kept


async def test_door_retry_kept_keeps_correlation_and_acks_rejected(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT

    response = await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert body_json(response) == {"status": "rejected"}
    assert _CORR_KEY in fake_redis.store  # the human can reply again


async def test_ladder_forward_error_releases_claim_then_retry_recovers(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_error = AnswerForwardError("callback forward failed: HTTP 500 from the interactions door")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert _DEDUPE_KEY not in fake_redis.store  # claim released for the retry
    assert _CORR_KEY in fake_redis.store

    # Slack's retry ladder redelivers the same event; the door recovered.
    channels.inbound_error = None
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    response = await slack_inbound(_signed(_event_body(event=_reply_event())))
    assert body_json(response) == {"status": "forwarded"}


async def test_transport_error_wrapped_by_ladder_releases_claim(fake_redis, channels):
    _seed_correlation(fake_redis)
    channels.inbound_error = AnswerForwardError("forwarding the answer to the door failed: connect error")

    with pytest.raises(AnswerForwardError, match="forwarding the answer"):
        await slack_inbound(_signed(_event_body(event=_reply_event())))

    assert _DEDUPE_KEY not in fake_redis.store
    assert _CORR_KEY in fake_redis.store


async def test_correlated_reply_without_text_raises_never_none(fake_redis, channels):
    _seed_correlation(fake_redis)
    event = _reply_event()
    del event["text"]

    with pytest.raises(ValueError, match="carries no text"):
        await slack_inbound(_signed(_event_body(event=event)))

    assert channels.inbound_calls == []  # never reached the ladder
    assert _DEDUPE_KEY not in fake_redis.store  # released so the retry reprocesses


async def test_event_callback_without_event_object_raises(fake_redis):
    with pytest.raises(ValueError, match="without an event object"):
        await slack_inbound(_signed(_event_body()))

    assert _DEDUPE_KEY not in fake_redis.store


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_reply_event(subtype="message_changed"), id="subtype-present"),
        pytest.param(_reply_event(type="reaction_added"), id="not-a-message"),
    ],
)
async def test_non_message_traffic_is_acked_ignored_never_bridged(fake_redis, http_script, stub_conversations, event):
    # Edits/joins carry a subtype; a non-message event is not text at all. Neither
    # forwards nor bridges.
    _seed_correlation(fake_redis)

    response = await slack_inbound(_signed(_event_body(event=event)))

    assert response.status_code == 200
    assert body_json(response) == {"status": "ignored"}
    assert http_script.requests == []
    assert stub_conversations.accept_calls == []
