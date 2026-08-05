"""The bridge branch: an uncorrelated human message becomes a conversation turn.

Correlation (a pending question) wins when it matches; every other verified human
message reaches ``conversations.accept``. Transport auth and event dedupe run
before either, so no message bridges without passing them first.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from tai42_contract.conversations import BlankInboundTextError
from tai42_kit.settings import reset_all_settings

from tai42_channel_slack.inbound import slack_inbound
from tests.conftest import (
    TEST_ALLOWED_RECIPIENT,
    TEST_BOT_USER_ID,
    TEST_DEFAULT_RECIPIENT,
    TEST_SIGNING_SECRET,
    body_json,
    make_request,
    signed_headers,
)

pytestmark = pytest.mark.usefixtures("slack_env")

_THREAD_TS = "1712345678.000100"
_CORR_KEY = f"channel:slack:corr:{_THREAD_TS}"
_CALLBACK = "http://gateway/api/interactions/callback/ticket-7"
_DEDUPE_KEY = "channel:slack:event:Ev001"


def _signed(body: bytes):
    return make_request(body, signed_headers(body, TEST_SIGNING_SECRET))


def _event_body(event: dict[str, Any], event_id: str = "Ev001") -> bytes:
    return json.dumps({"type": "event_callback", "event_id": event_id, "event": event}).encode()


def _message(**overrides: Any) -> dict[str, Any]:
    # A plain top-level human message (no thread_ts) in the default channel.
    event: dict[str, Any] = {
        "type": "message",
        "channel": TEST_DEFAULT_RECIPIENT,
        "text": "how do I reset my password?",
        "user": "U099CLIENT",
        "ts": "1712345679.000200",
    }
    event.update(overrides)
    return event


def _seed_correlation(fake_redis) -> None:
    fake_redis.store[_CORR_KEY] = _CALLBACK
    fake_redis.ttls[_CORR_KEY] = 300


async def test_uncorrelated_routed_message_reaches_accept_with_bridge_args(fake_redis, stub_conversations):
    # our_identity = bot user id, client_address = event channel, provider_message_id
    # = event_id.
    response = await slack_inbound(_signed(_event_body(_message(channel="C0OUTSIDE"))))

    assert body_json(response) == {"status": "accepted"}
    (call,) = stub_conversations.accept_calls
    assert call.channel == "slack"
    assert call.our_identity == TEST_BOT_USER_ID
    assert call.client_address == "C0OUTSIDE"
    assert call.text == "how do I reset my password?"
    assert call.provider_message_id == "Ev001"
    assert _DEDUPE_KEY in fake_redis.store  # processed: retries ack as duplicate


async def test_uncorrelated_unrouted_message_is_acked_no_turn(fake_redis, stub_conversations, caplog):
    # No route bound: accept refuses with a LookupError; the door acks and logs
    # rather than 500-storming a permanently-unrouted channel.
    stub_conversations.accept_error = LookupError("no route")

    import logging

    with caplog.at_level(logging.DEBUG):
        response = await slack_inbound(_signed(_event_body(_message())))

    assert body_json(response) == {"status": "ignored"}
    assert len(stub_conversations.accept_calls) == 1  # attempted, then refused
    assert _DEDUPE_KEY in fake_redis.store  # acked, claim kept
    assert any("no conversation route" in record.message for record in caplog.records)


async def test_uncorrelated_blank_message_is_acked_no_turn(fake_redis, stub_conversations, caplog):
    # A whitespace-only body passes the door's own text pre-filter, reaches the
    # bridge, and accept refuses with BlankInboundTextError: the door acks and logs
    # rather than 500-storming, and no turn is made.
    stub_conversations.accept_error = BlankInboundTextError("inbound text is blank")

    import logging

    with caplog.at_level(logging.DEBUG):
        response = await slack_inbound(_signed(_event_body(_message(text="   "))))

    assert body_json(response) == {"status": "ignored"}
    assert len(stub_conversations.accept_calls) == 1  # attempted, then refused
    assert _DEDUPE_KEY in fake_redis.store  # acked, claim kept
    assert any("blank" in record.message for record in caplog.records)


async def test_bridge_infrastructure_failure_propagates_and_releases_claim(fake_redis, stub_conversations):
    # An infrastructure failure (not a no-route refusal) must not ack-and-drop:
    # it propagates as a 500 so Slack redelivers, and the dedupe claim is released.
    stub_conversations.accept_error = RuntimeError("redis down")

    with pytest.raises(RuntimeError, match="redis down"):
        await slack_inbound(_signed(_event_body(_message())))

    assert _DEDUPE_KEY not in fake_redis.store


async def test_pending_question_reply_resolves_ask_and_never_bridges(fake_redis, http_script, stub_conversations):
    # Correlation precedence: a thread reply matching a pending question forwards
    # to the callback door; the bridge is never reached.
    _seed_correlation(fake_redis)
    http_script.results.append(httpx.Response(200))

    event = _message(thread_ts=_THREAD_TS, text="yes, do it")
    response = await slack_inbound(_signed(_event_body(event)))

    assert body_json(response) == {"status": "forwarded"}
    assert stub_conversations.accept_calls == []
    (forward,) = http_script.requests
    assert str(forward.url) == _CALLBACK


async def test_expired_thread_reply_bridges_not_ignored(fake_redis, http_script, stub_conversations):
    # A thread reply whose pending question expired (no correlation entry) is a
    # correlation MISS and must bridge, not silently ignore.
    event = _message(channel=TEST_ALLOWED_RECIPIENT, thread_ts=_THREAD_TS, text="following up")
    response = await slack_inbound(_signed(_event_body(event)))

    assert body_json(response) == {"status": "accepted"}
    assert http_script.requests == []  # no forward: nothing correlated
    (call,) = stub_conversations.accept_calls
    assert call.client_address == TEST_ALLOWED_RECIPIENT
    assert call.text == "following up"


@pytest.mark.parametrize(
    "event",
    [
        pytest.param(_message(bot_id="B0BOT"), id="bot-echo"),
        pytest.param(_message(user=TEST_BOT_USER_ID), id="self-authored"),
    ],
)
async def test_bot_and_self_messages_never_bridge(fake_redis, stub_conversations, event):
    # The bot's own posts (bot_id echo, or a message authored by the bot user id)
    # stay ignored — they must never re-enter the bridge as client traffic.
    response = await slack_inbound(_signed(_event_body(event)))

    assert body_json(response) == {"status": "ignored"}
    assert stub_conversations.accept_calls == []


async def test_textless_message_is_nothing_to_bridge(fake_redis, stub_conversations):
    # A plain message carrying no text (e.g. an attachment-only post) is nothing
    # to bridge: acked, never handed to accept.
    event = _message()
    del event["text"]

    response = await slack_inbound(_signed(_event_body(event)))

    assert body_json(response) == {"status": "ignored"}
    assert stub_conversations.accept_calls == []


async def test_signature_failure_short_circuits_before_bridge(fake_redis, stub_conversations):
    # Transport auth is first: an unsigned request is 401 and never reaches accept.
    response = await slack_inbound(make_request(_event_body(_message()), {}))

    assert response.status_code == 401
    assert stub_conversations.accept_calls == []


async def test_dedupe_hit_short_circuits_before_second_bridge(fake_redis, stub_conversations):
    # Event dedupe is before the bridge: a redelivered event acks as a duplicate
    # and does not call accept a second time.
    first = await slack_inbound(_signed(_event_body(_message())))
    second = await slack_inbound(_signed(_event_body(_message())))

    assert body_json(first) == {"status": "accepted"}
    assert body_json(second) == {"status": "duplicate"}
    assert len(stub_conversations.accept_calls) == 1


async def test_bot_user_id_unset_on_bridge_route_is_loud_misconfig(fake_redis, stub_conversations, monkeypatch):
    # A bridge route with no bot user id set: the branch that needs it as
    # our_identity raises loudly and releases the claim (Slack retries).
    monkeypatch.delenv("CHANNEL_SLACK_BOT_USER_ID")
    reset_all_settings()

    with pytest.raises(ValueError, match="CHANNEL_SLACK_BOT_USER_ID"):
        await slack_inbound(_signed(_event_body(_message())))

    assert stub_conversations.accept_calls == []
    assert _DEDUPE_KEY not in fake_redis.store


async def test_bot_user_id_unset_does_not_block_ask_user_path(fake_redis, http_script, stub_conversations, monkeypatch):
    # The ask_user path needs no bot user id: a correlated reply still forwards
    # when CHANNEL_SLACK_BOT_USER_ID is unset.
    monkeypatch.delenv("CHANNEL_SLACK_BOT_USER_ID")
    reset_all_settings()
    _seed_correlation(fake_redis)
    http_script.results.append(httpx.Response(200))

    event = _message(thread_ts=_THREAD_TS, text="yes")
    response = await slack_inbound(_signed(_event_body(event)))

    assert body_json(response) == {"status": "forwarded"}
    assert stub_conversations.accept_calls == []
