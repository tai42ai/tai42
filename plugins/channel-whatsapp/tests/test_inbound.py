"""The inbound webhook handler — GET verification, signature validation, dedupe,
correlation, bridge branch, forward policy, and delivery-status webhooks."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
from starlette.responses import Response
from tai42_contract.channels import AnswerForwardError, ChannelDeliveryError, InboundAnswerOutcome
from tai42_contract.conversations import BlankInboundTextError, DeliveryReceipt
from tai42_contract.interactions.models import LocationElement
from tai42_kit.settings import reset_all_settings

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_whatsapp.correlation import PendingQuestion, reserve_pending
from tai42_channel_whatsapp.flows import build_flow
from tai42_channel_whatsapp.inbound import (
    _CALLBACK_REJECTION_OPAQUE,
    _FLOW_BODY_MAX_CHARS,
    _FORM_REJECTION_LEAD,
    _FORM_UNPROCESSABLE,
    _MAX_FORM_REJECTIONS,
    _render_answer_for_bridge,
)

from .conftest import (
    PHONE_NUMBER_ID,
    WA_ID,
    FakeHttpx,
    FakeRedis,
    build_request,
    compute_signature,
    make_delivery,
    message_payload,
    response,
    signed_request,
    status_payload,
    verify_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")

_PATH = "/inbound"
_WAMID = "wamid.TESTID"
_CALLBACK = "https://app.example/api/interactions/callback/ticket-1"
_SEEN_KEY = f"channel:whatsapp:seen:{_WAMID}"
_PENDING_KEY = f"channel:whatsapp:pending:{PHONE_NUMBER_ID}:{WA_ID}"


@pytest.fixture
def handler(stub_app) -> Callable[..., Awaitable[Response]]:
    routes = [route for route in stub_app.http.routes if route.path == _PATH]
    assert len(routes) == 1
    route = routes[0]
    assert route.methods == ["GET", "POST"]
    assert route.authed is None
    return route.handler


_CONTACT_KEY = f"channel:whatsapp:known-contact:{PHONE_NUMBER_ID}:{WA_ID}"


async def _seed_pending(callback_url: str = _CALLBACK) -> None:
    delivery = make_delivery(callback_url=callback_url)
    await reserve_pending(PHONE_NUMBER_ID, WA_ID, delivery.callback_url, delivery.timeout_at)


async def _seed_pending_select(
    options: list[str], interaction_id: str = "int-1", callback_url: str = _CALLBACK
) -> None:
    delivery = make_delivery(callback_url=callback_url)
    await reserve_pending(
        PHONE_NUMBER_ID,
        WA_ID,
        delivery.callback_url,
        delivery.timeout_at,
        options=options,
        interaction_id=interaction_id,
    )


def interactive_payload(
    *,
    wamid: str = _WAMID,
    phone_number_id: str = PHONE_NUMBER_ID,
    wa_id: str = WA_ID,
    reply_type: str = "button_reply",
    reply_id: str = "int-1:0",
    title: str = "staging",
    description: str | None = None,
    interactive: dict | None = None,
) -> dict:
    """A signed-inbound envelope carrying one interactive (button/list) reply.

    ``interactive`` overrides the whole interactive object (for malformed-shape
    tests); otherwise a ``{reply_type: {id, title[, description]}}`` is built.
    """
    if interactive is None:
        reply: dict = {"id": reply_id, "title": title}
        if description is not None:
            reply["description"] = description
        interactive = {"type": reply_type, reply_type: reply}
    message = {"id": wamid, "from": wa_id, "type": "interactive", "interactive": interactive}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "15551112222", "phone_number_id": phone_number_id},
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


async def _pending_intact(fake_redis: FakeRedis) -> bool:
    return _PENDING_KEY in fake_redis.store


# --- GET verification handshake -----------------------------------------------


async def test_verify_valid_token_echoes_challenge(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    result = await handler(verify_request(challenge="12345"))

    assert result.status_code == 200
    assert result.body == b"12345"


async def test_verify_bad_token_is_403(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    result = await handler(verify_request(token="wrong-token", challenge="12345"))

    assert result.status_code == 403


async def test_verify_non_ascii_token_is_403_not_500(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A non-ASCII hub.verify_token must map to a 403 mismatch, never a
    # compare_digest TypeError surfacing as a 500; the challenge is not echoed.
    query = b"hub.mode=subscribe&hub.verify_token=\xff&hub.challenge=12345"
    request = build_request(method="GET", headers={"host": "public.example"}, query=query)

    result = await handler(request)

    assert result.status_code == 403
    assert result.body != b"12345"


async def test_verify_wrong_mode_is_403(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    result = await handler(verify_request(mode="unsubscribe"))

    assert result.status_code == 403


async def test_verify_missing_challenge_is_400(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    result = await handler(verify_request(challenge=None))

    assert result.status_code == 400


async def test_verify_unset_token_is_500(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_VERIFY_TOKEN")
    reset_all_settings()

    result = await handler(verify_request(challenge="12345"))

    assert result.status_code == 500
    assert json.loads(result.body) == {"error": "channel misconfigured"}


# --- POST signature (fail-closed) ---------------------------------------------


async def _assert_rejected(handler, request, status: int = 401):
    result = await handler(request)
    assert result.status_code == status
    return result


async def test_missing_signature_header_rejected(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _assert_rejected(handler, signed_request(message_payload(), omit_signature=True))
    assert stub_app.conversations.accept_calls == []


async def test_wrong_secret_signature_rejected(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _assert_rejected(handler, signed_request(message_payload(), secret="not-the-secret"))
    assert stub_app.conversations.accept_calls == []


async def test_malformed_signature_header_rejected(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A header not in sha256=<hex> form is rejected before any HMAC compare.
    await _assert_rejected(handler, signed_request(message_payload(), signature="deadbeef"))
    assert stub_app.conversations.accept_calls == []


async def test_non_hex_signature_rejected_401_not_500(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A sha256=<non-hex/non-ASCII> header must decode-fail to a 401, never raise a
    # compare_digest TypeError that surfaces as a 500.
    await _assert_rejected(handler, signed_request(message_payload(), signature="sha256=\xff"))
    await _assert_rejected(handler, signed_request(message_payload(), signature="sha256=zz"))
    assert stub_app.conversations.accept_calls == []


async def test_tampered_body_rejected(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Sign one body, deliver another: the signature is over the RAW bytes.
    signature = compute_signature(json.dumps(message_payload(text="yes")).encode("utf-8"))
    request = signed_request(message_payload(text="no way"), signature=signature)
    await _assert_rejected(handler, request)
    assert stub_app.conversations.accept_calls == []


async def test_unset_app_secret_is_500(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_APP_SECRET")
    reset_all_settings()

    # Operator misconfiguration is a logged, constant 500 — never a 401 that reads
    # like an ordinary bad signature, never any processing.
    result = await handler(signed_request(message_payload()))

    assert result.status_code == 500
    assert json.loads(result.body) == {"error": "channel misconfigured"}
    assert stub_app.conversations.accept_calls == []


async def test_oversized_body_413_before_any_signature_work(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # No signature header at all, yet the response is 413 (not 401): the bounded
    # read runs BEFORE any HMAC work.
    request = build_request(
        method="POST",
        chunks=[b"x" * (512 * 1024), b"y" * (512 * 1024), b"z"],
        headers={"host": "public.example", "content-type": "application/json"},
    )
    await _assert_rejected(handler, request, status=413)


async def test_invalid_json_after_valid_signature_is_400(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    body = b"{not json"
    request = build_request(
        method="POST",
        body=body,
        headers={
            "host": "public.example",
            "content-type": "application/json",
            "x-hub-signature-256": compute_signature(body),
        },
    )
    result = await handler(request)
    assert result.status_code == 400


# --- Message flow: dedupe, correlation, bridge --------------------------------


async def test_wamid_dedupe_short_circuits_before_bridge(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    fake_redis.store[_SEEN_KEY] = "1"

    result = await handler(signed_request(message_payload()))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []
    assert not fake_httpx.calls


async def test_pending_question_resolves_before_bridge(handler, stub_app, channels, fake_redis: FakeRedis):
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(message_payload(text="yes please")))

    assert result.status_code == 200
    # The reply is handed to the shared ladder with the pair key + the reply text; a
    # text ask does not own its retry notice (core would send it).
    assert len(channels.inbound_calls) == 1
    call = channels.inbound_calls[0]
    assert call.correlation_key == f"{PHONE_NUMBER_ID}:{WA_ID}"
    assert call.answer == "yes please"
    assert call.bridge.owns_retry_notice is False
    assert stub_app.conversations.accept_calls == []  # the caller's bridge was NOT reached
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert _SEEN_KEY in fake_redis.store


async def test_expired_ask_fallback_bridge_carries_message_params(handler, stub_app, channels, fake_redis: FakeRedis):
    # A reply that IS an answer at decode-peek, whose ask expires before the ladder's
    # own peek (NO_CORRELATION): the fallback bridge is the bridge path, so it carries
    # the same message-level params (reply-to context here) the caller's own bridge
    # branch would have carried.
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION

    message = {
        "id": "wamid.EXP1",
        "from": WA_ID,
        "type": "text",
        "text": {"body": "yes please"},
        "context": {"id": "wamid.QUOTED"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    assert len(stub_app.conversations.accept_calls) == 1
    call = stub_app.conversations.accept_calls[0]
    assert call["text"] == "yes please"
    assert call["params"] == {"context_message_id": "wamid.QUOTED"}


async def test_answer_is_body_verbatim_minus_outer_whitespace(handler, channels, fake_redis: FakeRedis):
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    await handler(signed_request(message_payload(text="  yes please \n")))

    assert channels.inbound_calls[0].answer == "yes please"


async def test_uncorrelated_routed_inbound_calls_accept_with_verbatim_args(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    result = await handler(signed_request(message_payload(text="ship it")))

    assert result.status_code == 200
    assert not fake_httpx.calls  # bridge does not use the ask_user forward
    assert stub_app.conversations.accept_calls == [
        {
            "channel": "whatsapp",
            "our_identity": PHONE_NUMBER_ID,
            "client_address": WA_ID,
            # The provider attests the wa_id, so it is also the accountable cap key.
            "cap_key": WA_ID,
            "text": "ship it",
            "provider_message_id": _WAMID,
            "params": None,
            "form": None,  # a plain text message carries no structured form
            "attachments": None,
            "location": None,
        }
    ]
    assert _SEEN_KEY in fake_redis.store


async def test_uncorrelated_unrouted_inbound_logged_ack_no_turn(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    stub_app.conversations.accept_error = LookupError("no channel conversation route matches")

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(message_payload()))

    assert result.status_code == 200
    assert not fake_httpx.calls  # no ask_user forward
    assert len(stub_app.conversations.accept_calls) == 1  # the bridge was attempted
    assert any("unrouted" in record.message for record in caplog.records)
    assert _SEEN_KEY in fake_redis.store  # replay of the same wamid dedupes


async def test_uncorrelated_blank_inbound_logged_ack_no_turn(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A whitespace-only body passes the door's own text pre-filter, reaches the
    # bridge, and accept() raises BlankInboundTextError: the drop is logged and the
    # webhook still 200-acks (no 5xx that would make Meta retry-storm), no turn made.
    stub_app.conversations.accept_error = BlankInboundTextError("inbound text is blank")

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(message_payload(text="   ")))

    assert result.status_code == 200
    assert not fake_httpx.calls  # no ask_user forward
    assert len(stub_app.conversations.accept_calls) == 1  # the bridge was attempted, no turn produced
    assert any("blank" in record.message for record in caplog.records)
    assert _SEEN_KEY in fake_redis.store  # replay of the same wamid dedupes


async def test_expired_question_reply_reaches_bridge(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # An expired question's key has elapsed (gone from the store); the reply is a
    # correlation MISS and must reach the bridge, not be dropped.
    await _seed_pending()
    del fake_redis.store[_PENDING_KEY]  # simulate TTL expiry

    result = await handler(signed_request(message_payload(text="late reply")))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == [
        {
            "channel": "whatsapp",
            "our_identity": PHONE_NUMBER_ID,
            "client_address": WA_ID,
            "cap_key": WA_ID,
            "text": "late reply",
            "provider_message_id": _WAMID,
            "params": None,
            "form": None,
            "attachments": None,
            "location": None,
        }
    ]


async def test_unknown_message_type_acked_no_turn(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A type the channel does not model (a future/system type) is not bridged — logged and
    # 200-acked, never consumed (nothing to dedupe).
    with caplog.at_level("INFO"):
        result = await handler(signed_request(message_payload(msg_type="system")))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []  # no bridge turn
    assert _SEEN_KEY not in fake_redis.store  # not consumed — nothing to dedupe


async def test_message_missing_id_is_skipped_and_acked(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A message with no id is odd (Meta always sends one); in a batch it is logged
    # and skipped, and the POST is 200-acked — never a per-message 400 that would
    # abandon the batch's other messages.
    payload = message_payload()
    del payload["entry"][0]["changes"][0]["value"]["messages"][0]["id"]
    with caplog.at_level("WARNING"):
        result = await handler(signed_request(payload))
    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []
    assert any("missing a string id" in record.message for record in caplog.records)


async def test_signature_failure_short_circuits_before_bridge(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    result = await handler(signed_request(message_payload(), secret="other-secret"))

    assert result.status_code == 401
    assert stub_app.conversations.accept_calls == []
    assert _SEEN_KEY not in fake_redis.store


async def test_bridge_overflow_propagates_and_does_not_dedupe(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A retryable/infrastructure failure from accept() (not a LookupError) propagates
    # as a 5xx so Meta redelivers; the wamid is NOT marked seen.
    stub_app.conversations.accept_error = RuntimeError("per-thread FIFO is full")

    with pytest.raises(RuntimeError, match="FIFO is full"):
        await handler(signed_request(message_payload()))

    assert _SEEN_KEY not in fake_redis.store


async def test_multi_number_two_phone_number_ids_route_independently(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # One credential fronts two phone_number_ids; each inbound bridges under its
    # own our_identity.
    await handler(signed_request(message_payload(wamid="wamid.A", phone_number_id="11111111111111", text="to A")))
    await handler(signed_request(message_payload(wamid="wamid.B", phone_number_id="22222222222222", text="to B")))

    assert [call["our_identity"] for call in stub_app.conversations.accept_calls] == [
        "11111111111111",
        "22222222222222",
    ]
    assert [call["provider_message_id"] for call in stub_app.conversations.accept_calls] == ["wamid.A", "wamid.B"]


# --- Batching: every message and every status is processed ---------------------


async def test_batched_messages_all_bridge(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A single value carries two messages; BOTH must bridge (not just messages[0]),
    # or the second is permanently lost after the 200-ack.
    payload = message_payload(wamid="wamid.M1", text="first")
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"].append({"id": "wamid.M2", "from": WA_ID, "type": "text", "text": {"body": "second"}})

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M1", "wamid.M2"]
    assert [c["text"] for c in stub_app.conversations.accept_calls] == ["first", "second"]


async def test_batched_message_lookuperror_continues_to_next(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An unrouted (LookupError) first message is logged+skipped; the second is still
    # attempted (a per-message drop must not abandon the rest of the batch).
    stub_app.conversations.accept_error = LookupError("no channel conversation route matches")
    payload = message_payload(wamid="wamid.M1", text="first")
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"].append({"id": "wamid.M2", "from": WA_ID, "type": "text", "text": {"body": "second"}})

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M1", "wamid.M2"]


async def test_batched_failing_reply_does_not_starve_independent_bridge(
    handler, stub_app, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # m1 is a correlated reply whose callback door persistently 5xx's; m2 is an
    # independent bridge message for a different wa_id. m1's propagating failure
    # must NOT abandon m2: m2 bridges on the first attempt, the batch still 5xx's
    # so the un-acked m1 redelivers, and on redelivery m2 is wamid-skipped (never
    # double-bridged). Reverting to an early raise leaves accept_calls empty here.
    await _seed_pending()  # pending for (PHONE_NUMBER_ID, WA_ID) — m1 correlates
    payload = message_payload(wamid="wamid.M1", text="the answer")
    value = payload["entry"][0]["changes"][0]["value"]
    value["messages"].append({"id": "wamid.M2", "from": "15559990002", "type": "text", "text": {"body": "independent"}})
    # m1 correlates (a pending exists) so it reaches the ladder, which raises; m2 has no
    # pending so it never reaches the ladder — it bridges independently.
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: down")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(payload))

    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M2"]  # m2 committed
    assert await _pending_intact(fake_redis)  # m1's ask kept — it retries on redelivery
    assert "channel:whatsapp:seen:wamid.M1" not in fake_redis.store  # m1 un-acked
    assert "channel:whatsapp:seen:wamid.M2" in fake_redis.store  # m2 acked

    # Redelivery: m1 fails again (ladder still raises), m2 is dedupe-skipped (no re-bridge).
    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(payload))

    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M2"]  # still once
    assert len(channels.inbound_calls) == 2  # m1 reached the ladder twice; m2 never (no pending)


async def test_status_infra_failure_reraises_but_later_message_commits(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A status whose record_delivery_status raises a non-LookupError (infra) 5xx's
    # so Meta redelivers — but a message batched in the same value still bridges
    # and commits its dedupe first (no head-of-line starvation on the status path).
    stub_app.conversations.status_error = RuntimeError("delivery store unavailable")
    payload = message_payload(wamid="wamid.MSG", text="hello")
    value = payload["entry"][0]["changes"][0]["value"]
    value["statuses"] = [{"id": "wamid.OUT", "status": "delivered", "recipient_id": WA_ID}]

    with pytest.raises(RuntimeError, match="delivery store unavailable"):
        await handler(signed_request(payload))

    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.MSG"]  # committed
    assert "channel:whatsapp:seen:wamid.MSG" in fake_redis.store


async def test_batched_statuses_two_entries_all_settle(handler, stub_app):
    # Two entries, each carrying one status; BOTH must settle (the status path must
    # iterate every entry/value, not just the first).
    payload = status_payload(wamid="wamid.S1", state="delivered")
    second = status_payload(wamid="wamid.S2", state="failed")
    payload["entry"].append(second["entry"][0])

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert [c["provider_message_id"] for c in stub_app.conversations.status_calls] == ["wamid.S1", "wamid.S2"]
    assert [c["status"] for c in stub_app.conversations.status_calls] == [
        DeliveryReceipt.DELIVERED,
        DeliveryReceipt.FAILED,
    ]


async def test_type_confused_envelope_is_not_5xx(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A well-signed but type-confused envelope (non-object entry/change/metadata,
    # non-list changes, non-object message and status items) must be 200-acked,
    # never AttributeError -> 500.
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            "not-a-dict",
            {"id": "A", "changes": "not-a-list"},
            {
                "id": "B",
                "changes": [
                    "not-a-dict",
                    {
                        "field": "messages",
                        "value": {"metadata": ["nope"], "messages": ["not-a-dict", 42], "statuses": ["not-a-dict"]},
                    },
                ],
            },
        ],
    }

    result = await handler(signed_request(payload))

    assert result.status_code < 500
    assert stub_app.conversations.accept_calls == []
    assert stub_app.conversations.status_calls == []


async def test_dict_payload_with_non_list_entry_is_acked(handler, stub_app):
    # A well-signed object payload whose "entry" is not a list carries nothing to
    # act on — 200-acked, no effect.
    result = await handler(signed_request({"object": "whatsapp_business_account", "entry": "not-a-list"}))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []


# --- Forward status policy (correlated reply) ---------------------------------


async def test_ladder_forward_error_raises_and_does_not_dedupe(handler, channels, fake_redis: FakeRedis):
    # A door 5xx / 401 / transport fault surfaces as AnswerForwardError from the ladder
    # (which keeps the correlation for the retry); the plugin lets it propagate and does
    # NOT mark the wamid seen, so Meta's redelivery re-runs the ladder.
    await _seed_pending()
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: oops")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(message_payload()))

    assert await _pending_intact(fake_redis)  # the ladder kept it (stub left it intact)
    assert _SEEN_KEY not in fake_redis.store  # NOT marked seen — the retry must not dedupe away


async def test_ladder_bridged_text_reply_acks_and_renders_bridge_text(handler, channels, fake_redis: FakeRedis):
    # A BRIDGED outcome (the ask is gone / a hard mismatch — the ladder already bridged
    # the reply internally, carrying the text under the wamid dedupe key) acks 200 and
    # marks the wamid seen. The channel supplies the faithful bridge text.
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    result = await handler(signed_request(message_payload(text="yes please")))

    assert result.status_code == 200
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert _SEEN_KEY in fake_redis.store
    assert channels.inbound_calls[0].bridge.bridge_text == "yes please"
    assert channels.inbound_calls[0].bridge.provider_message_id == _WAMID

    # Meta redelivers the same wamid: dedupe short-circuits — the ladder is not consulted again.
    redelivery = await handler(signed_request(message_payload(text="yes please")))
    assert redelivery.status_code == 200
    assert len(channels.inbound_calls) == 1  # still once


async def test_bridged_select_tap_renders_the_option_label(handler, channels, fake_redis: FakeRedis):
    # A select tap resolves to its option label in the channel, which is what the
    # ladder's bridge would carry when the interaction is terminally gone.
    await _seed_pending_select(options=["staging", "production"])
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    result = await handler(signed_request(interactive_payload(reply_id="int-1:1", title="production")))

    assert result.status_code == 200
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert channels.inbound_calls[0].answer == "production"  # the tap resolved to options[1]
    assert channels.inbound_calls[0].bridge.bridge_text == "production"
    assert channels.inbound_calls[0].bridge.provider_message_id == _WAMID
    assert _SEEN_KEY in fake_redis.store


async def test_bridged_form_renders_readable_fields(handler, channels, fake_redis: FakeRedis):
    # A completed Flow form's bridge text renders the submitted field/value pairs
    # readably (never a raw JSON dump), which the ladder would carry on a gone ask.
    await _seed_pending_form()
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "ship it", "qty": "7"})))

    assert result.status_code == 200
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert channels.inbound_calls[0].answer == {"note": "ship it", "qty": 7}  # coerced dict forwarded
    bridged = channels.inbound_calls[0].bridge.bridge_text
    assert "note: ship it" in bridged
    assert "qty: 7" in bridged  # coerced to int, rendered as a labelled field line
    assert "{" not in bridged  # not a raw JSON dump
    assert channels.inbound_calls[0].bridge.provider_message_id == _WAMID


def test_render_empty_form_answer_bridges_a_non_empty_string():
    # A completed-but-empty Flow form renders no field lines; the fallback is a faithful
    # compact dump so the bridge is handed a non-blank string (the accept door rejects
    # blank text) and the reply is never dropped.
    pending = PendingQuestion(callback_url=_CALLBACK, timeout_at=datetime.now(UTC), schema={"properties": {}})
    rendered = _render_answer_for_bridge({}, pending)
    assert rendered != ""
    assert rendered == "{}"


def test_render_form_answer_with_a_nested_value_uses_json_not_repr():
    # A non-scalar field value renders as compact JSON, not a Python ``repr``; a scalar
    # field is unchanged.
    pending = PendingQuestion(
        callback_url=_CALLBACK,
        timeout_at=datetime.now(UTC),
        schema={"properties": {"tags": {"title": "Tags"}, "note": {"title": "Note"}}},
    )
    rendered = _render_answer_for_bridge({"tags": ["a", "b"], "note": "ship it"}, pending)
    assert 'Tags: ["a", "b"]' in rendered
    assert "['a', 'b']" not in rendered  # never a Python repr
    assert "Note: ship it" in rendered  # scalar rendering unchanged


def test_render_form_answer_with_a_boolean_renders_json_not_python_repr():
    # A boolean renders through JSON (``true``/``false``) — the same standard-faithful
    # convention the web channel uses — never Python's ``True``/``False`` repr, even
    # though ``bool`` is a subclass of ``int``. The source value stays a real bool: the
    # structured form carried alongside the text is untouched by rendering.
    pending = PendingQuestion(
        callback_url=_CALLBACK,
        timeout_at=datetime.now(UTC),
        schema={"properties": {"subscribed": {"title": "Subscribed"}, "declined": {"title": "Declined"}}},
    )
    answer = {"subscribed": True, "declined": False}
    rendered = _render_answer_for_bridge(answer, pending)
    assert "Subscribed: true" in rendered
    assert "Declined: false" in rendered
    assert "True" not in rendered  # never a Python repr
    assert "False" not in rendered
    assert answer["subscribed"] is True  # unmutated real bools
    assert answer["declined"] is False


async def test_ladder_forward_error_does_not_bridge(handler, stub_app, channels, fake_redis: FakeRedis):
    # A raised AnswerForwardError (5xx/transport) must NOT be converted into a bridge:
    # the ladder kept the correlation and the plugin re-raises for Meta's retry.
    await _seed_pending()
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: oops")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(message_payload()))

    assert stub_app.conversations.accept_calls == []  # never bridged
    assert await _pending_intact(fake_redis)  # kept for the retry


async def test_text_ask_retry_kept_acks_and_keeps_correlation(handler, channels, fake_redis: FakeRedis):
    # RETRY_KEPT on a text ask (owns_retry_notice=False, so the CORE sent the guest
    # notice): the correlation is kept and the wamid is marked seen — redelivering the
    # same body would be rejected again.
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT

    result = await handler(signed_request(message_payload()))

    assert result.status_code == 200
    assert channels.inbound_calls[0].bridge.owns_retry_notice is False  # core owns the text-ask notice
    assert await _pending_intact(fake_redis)  # kept — the human can reply again
    assert _SEEN_KEY in fake_redis.store


# --- Delivery-status webhooks -------------------------------------------------


@pytest.mark.parametrize("state", ["sent", "delivered"])
async def test_status_sent_or_delivered_records_delivered(handler, stub_app, state: str):
    result = await handler(signed_request(status_payload(wamid="wamid.OUT", state=state)))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == [
        {"channel": "whatsapp", "provider_message_id": "wamid.OUT", "status": DeliveryReceipt.DELIVERED}
    ]


async def test_status_failed_records_failed_loudly(handler, stub_app, caplog: pytest.LogCaptureFixture):
    with caplog.at_level("WARNING"):
        result = await handler(signed_request(status_payload(wamid="wamid.OUT", state="failed")))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == [
        {"channel": "whatsapp", "provider_message_id": "wamid.OUT", "status": DeliveryReceipt.FAILED}
    ]
    assert any("delivery failure" in record.message for record in caplog.records)


async def test_status_read_is_ignored(handler, stub_app):
    result = await handler(signed_request(status_payload(wamid="wamid.OUT", state="read")))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == []  # informational — nothing recorded


async def test_status_unknown_state_is_ignored(handler, stub_app):
    result = await handler(signed_request(status_payload(wamid="wamid.OUT", state="deleted")))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == []


async def test_status_unknown_wamid_is_benign_200(handler, stub_app, caplog: pytest.LogCaptureFixture):
    # record_delivery_status raises LookupError for a wamid the bridge does not
    # track; the route acks rather than 5xx-ing the provider for a message we do
    # not own.
    stub_app.conversations.status_error = LookupError("outbound id wamid.OUT maps to no answer record")

    with caplog.at_level("INFO"):
        result = await handler(signed_request(status_payload(wamid="wamid.OUT", state="delivered")))

    assert result.status_code == 200
    assert any("untracked message wamid.OUT" in record.message for record in caplog.records)


async def test_status_flow_send_hit_delivered_posts_receipt_no_untracked_log(
    handler, stub_app, caplog: pytest.LogCaptureFixture
):
    # The bridge does not own the id (LookupError), but it is a flow send: the flow-send
    # receipt seam resolves it and posts the receipt onto the trace, so NO untracked log.
    stub_app.conversations.status_error = LookupError("outbound id wamid.FLOW maps to no answer record")
    stub_app.channels.flow_receipt_result = True

    with caplog.at_level("INFO"):
        result = await handler(signed_request(status_payload(wamid="wamid.FLOW", state="delivered")))

    assert result.status_code == 200
    assert stub_app.channels.flow_receipt_calls == [
        {
            "channel": "whatsapp",
            "provider_message_id": "wamid.FLOW",
            "status": DeliveryReceipt.DELIVERED,
            "errors": None,
        }
    ]
    assert not any("untracked message wamid.FLOW" in record.message for record in caplog.records)


async def test_status_flow_send_hit_failed_forwards_errors(handler, stub_app):
    stub_app.conversations.status_error = LookupError("outbound id wamid.FLOW maps to no answer record")
    stub_app.channels.flow_receipt_result = True

    result = await handler(signed_request(status_payload(wamid="wamid.FLOW", state="failed")))

    assert result.status_code == 200
    (call,) = stub_app.channels.flow_receipt_calls
    assert call["status"] is DeliveryReceipt.FAILED
    # The provider's error detail rides the receipt so the trace event carries it.
    assert call["errors"] == [{"code": 131047, "title": "Re-engagement message"}]


async def test_status_bad_signature_is_401(handler, stub_app):
    result = await handler(signed_request(status_payload(), secret="other-secret"))

    assert result.status_code == 401
    assert stub_app.conversations.status_calls == []


async def test_status_entry_missing_fields_is_skipped(handler, stub_app, caplog: pytest.LogCaptureFixture):
    payload = status_payload()
    del payload["entry"][0]["changes"][0]["value"]["statuses"][0]["status"]

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == []
    assert any("missing string id/status" in record.message for record in caplog.records)


async def test_status_type_confused_field_is_acked_not_500(handler, stub_app, caplog: pytest.LogCaptureFixture):
    # A well-signed status whose `status` (and `id`) is an unhashable list must be
    # 200-acked with no record_delivery_status call — never a `_DELIVERY_RECEIPTS`
    # dict-key TypeError('unhashable type: list') surfacing as a 500 that blocks
    # every legitimate item batched alongside it.
    payload = status_payload()
    status = payload["entry"][0]["changes"][0]["value"]["statuses"][0]
    status["id"] = ["wamid.OUT"]
    status["status"] = ["failed"]

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert stub_app.conversations.status_calls == []
    assert any("missing string id/status" in record.message for record in caplog.records)


# --- Form (Flow) replies: nfm_reply -------------------------------------------

_FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "note": {"type": "string"},
        "qty": {"type": "integer"},
        "amount": {"type": "number"},
        "agree": {"type": "boolean"},
    },
    "required": ["note"],
}


_FORM_QUESTION = "Deploy to prod?"
_WABA_ID = "WABA-100"


def _flow_cache_key() -> str:
    _, schema_hash = build_flow(_FORM_SCHEMA)
    return f"channel:whatsapp:flow:{_WABA_ID}:{schema_hash}"


@pytest.fixture
def waba_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Add the WABA id the form re-send path resolves the cached flow id under."""
    monkeypatch.setenv("CHANNEL_WHATSAPP_WABA_ID", _WABA_ID)
    reset_all_settings()


def _seed_flow_cache(fake_redis: FakeRedis, flow_id: str = "flow-cached") -> None:
    """The published-flow cache entry the original form send left behind."""
    fake_redis.store[_flow_cache_key()] = flow_id


def _set_stored_rejections(fake_redis: FakeRedis, rejections: int) -> None:
    """Rewrite the pending form record's rejection counter in place (drive the cap)."""
    data = json.loads(fake_redis.store[_PENDING_KEY])
    data["rejections"] = rejections
    fake_redis.store[_PENDING_KEY] = json.dumps(data)


def _stored_rejections(fake_redis: FakeRedis) -> int:
    """The pending form record's current rejection counter."""
    return json.loads(fake_redis.store[_PENDING_KEY])["rejections"]


async def _seed_pending_form(
    interaction_id: str = "int-1", callback_url: str = _CALLBACK, question: str = _FORM_QUESTION
) -> None:
    delivery = make_delivery(callback_url=callback_url, question=question)
    await reserve_pending(
        PHONE_NUMBER_ID,
        WA_ID,
        delivery.callback_url,
        delivery.timeout_at,
        interaction_id=interaction_id,
        schema=_FORM_SCHEMA,
        question=delivery.question,
    )


def form_reply_payload(response: dict, *, wamid: str = _WAMID, wa_id: str = WA_ID) -> dict:
    """A signed-inbound envelope carrying one completed Flow form (``nfm_reply``);
    ``response`` is serialized into ``response_json`` exactly as Meta delivers it."""
    interactive = {"type": "nfm_reply", "nfm_reply": {"response_json": json.dumps(response)}}
    message = {"id": wamid, "from": wa_id, "type": "interactive", "interactive": interactive}
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "15551112222", "phone_number_id": PHONE_NUMBER_ID},
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def _flow_accepted(wamid: str = "wamid.RESEND") -> httpx.Response:
    """A Cloud-API accept for a re-sent Flow message."""
    return response(200, json={"messages": [{"id": wamid}]})


async def test_form_reply_forwards_coerced_answer_dict(handler, channels, fake_redis: FakeRedis):
    await _seed_pending_form()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(
        signed_request(
            form_reply_payload({"flow_token": "int-1", "note": "ship it", "qty": "7", "amount": "3.5", "agree": True})
        )
    )

    assert result.status_code == 200
    # flow_token stripped; qty string→int; amount string→float; agree bool passthrough; note string.
    assert channels.inbound_calls[0].answer == {"note": "ship it", "qty": 7, "amount": 3.5, "agree": True}
    # A form ask owns its retry notice — its correction surface is a re-opened Flow.
    assert channels.inbound_calls[0].bridge.owns_retry_notice is True
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert _SEEN_KEY in fake_redis.store


async def test_form_reply_coerces_string_boolean(handler, channels, fake_redis: FakeRedis):
    await _seed_pending_form()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x", "agree": "true"})))

    assert channels.inbound_calls[0].answer == {"note": "x", "agree": True}


async def test_form_reply_bad_coercion_forwards_raw_then_re_sends_flow(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A decimal in an integer field is forwarded raw (int("3.5") fails); the ladder's
    # RETRY_KEPT on this form ask recovers by re-sending a fresh Flow (the channel owns
    # the guest correction here — the ladder sent no notice).
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    # The ladder returns the door's own field-naming reason on RETRY_KEPT.
    channels.inbound_retry_reason = "answer at qty: 3.5 is not an integer"
    channels.inbound_retry_field = "qty"
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x", "qty": "3.5"})))

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == {"note": "x", "qty": "3.5"}  # raw, uncoerced, handed to the ladder
    resend = fake_httpx.calls[0]["json"]  # the ONLY send now — the answer forward is the ladder's
    assert resend["interactive"]["type"] == "flow"
    assert resend["interactive"]["action"]["parameters"]["flow_token"] == "int-1"
    assert resend["interactive"]["action"]["parameters"]["flow_id"] == "flow-cached"
    body = resend["interactive"]["body"]["text"]
    assert _FORM_QUESTION in body  # the question is repeated
    assert "qty" in body  # the door's field-naming reason rides the re-sent Flow body (parity restored)
    assert await _pending_intact(fake_redis)  # kept for the re-submission
    assert _stored_rejections(fake_redis) == 1


@pytest.mark.parametrize("raw_number", ["1e999", "nan", "inf"])
async def test_form_reply_non_finite_number_forwarded_raw(
    raw_number: str, waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A value parsing to inf/nan passes jsonschema yet serializes to null downstream;
    # it is forwarded raw so the door 400s and the reply is recovered by a fresh Flow.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(
        signed_request(form_reply_payload({"flow_token": "int-1", "note": "x", "amount": raw_number}))
    )

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == {"note": "x", "amount": raw_number}  # raw, uncoerced
    assert fake_httpx.calls[0]["json"]["interactive"]["type"] == "flow"  # recovered by a fresh Flow
    assert await _pending_intact(fake_redis)
    assert _stored_rejections(fake_redis) == 1


async def test_form_retry_kept_re_sends_fresh_flow(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # RETRY_KEPT on a form ask recovers by re-sending a fresh Flow for the SAME
    # interaction: same flow_token, the cached flow id, a generic error line, the
    # correlation kept alive with the rejection counted, and a single guest message.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = "note: must not be blank"
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    assert len(fake_httpx.calls) == 1  # the fresh Flow send only — the answer forward is the ladder's
    assert fake_httpx.calls[0]["url"].endswith(f"/{PHONE_NUMBER_ID}/messages")  # the fresh Flow send
    resend = fake_httpx.calls[0]["json"]
    assert resend["interactive"]["type"] == "flow"
    params = resend["interactive"]["action"]["parameters"]
    assert params["flow_token"] == "int-1"  # same interaction — reply matching unchanged
    assert params["flow_id"] == "flow-cached"  # reuses the cached flow id
    # The door's OWN reason rides the body (restored parity), not a generic line.
    assert resend["interactive"]["body"]["text"] == (
        f"{_FORM_QUESTION}\n\n{_FORM_REJECTION_LEAD} note: must not be blank"
    )
    assert _stored_rejections(fake_redis) == 1  # counter incremented
    assert await _pending_intact(fake_redis)  # pending kept
    assert _SEEN_KEY in fake_redis.store


async def test_form_retry_kept_without_reason_uses_opaque_line(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # When the door gave no usable reason (retry_reason None), the re-sent Flow falls
    # back to the fixed opaque line — the guest is never shown an intermediary's content.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = None
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    body = fake_httpx.calls[0]["json"]["interactive"]["body"]["text"]
    assert body == f"{_FORM_QUESTION}\n\n{_FORM_REJECTION_LEAD} {_CALLBACK_REJECTION_OPAQUE}"


async def test_form_rejection_long_question_drops_question_keeps_error(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A question long enough to overflow Meta's 1024-char interactive.body.text cap is
    # dropped WHOLE from the re-sent Flow body (never a mid-string ellipsis): the fresh
    # Flow re-presents the fields, so the body carries only the bounded lead+error tail
    # — which always fits — and the send succeeds with counter/pending unchanged.
    long_question = "Q" * (_FLOW_BODY_MAX_CHARS + 100)
    await _seed_pending_form(question=long_question)
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = "note: must not be blank"
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    body = fake_httpx.calls[0]["json"]["interactive"]["body"]["text"]
    assert body == f"{_FORM_REJECTION_LEAD} note: must not be blank"  # tail only — the question is gone
    assert long_question not in body  # no question
    assert not body.startswith("Q")  # not even a chopped prefix of it
    assert "…" not in body  # no ellipsis
    assert "..." not in body
    assert len(body) <= _FLOW_BODY_MAX_CHARS  # fits the vendor cap
    assert _stored_rejections(fake_redis) == 1  # counter incremented, same as the short-question path
    assert await _pending_intact(fake_redis)  # pending kept
    assert _SEEN_KEY in fake_redis.store


async def test_form_rejection_body_at_cap_boundary_keeps_question(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The cap is inclusive: a body whose length is EXACTLY the cap keeps the question,
    # and one char more would drop it. Pins the <= boundary against the constant.
    error = "note: must not be blank"
    tail = f"{_FORM_REJECTION_LEAD} {error}"
    question = "Q" * (_FLOW_BODY_MAX_CHARS - len(tail) - len("\n\n"))
    await _seed_pending_form(question=question)
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    channels.inbound_retry_reason = error
    fake_httpx.responses.append(_flow_accepted())

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert result.status_code == 200
    body = fake_httpx.calls[0]["json"]["interactive"]["body"]["text"]
    assert body == f"{question}\n\n{tail}"  # question kept at exactly the cap
    assert len(body) == _FLOW_BODY_MAX_CHARS
    assert _stored_rejections(fake_redis) == 1
    assert await _pending_intact(fake_redis)


async def test_form_rejection_cap_stops_re_send_and_bridges(
    waba_env,
    stub_app,
    handler,
    channels,
    fake_redis: FakeRedis,
    fake_httpx: FakeHttpx,
    caplog: pytest.LogCaptureFixture,
):
    # Two RETRY_KEPT rejections under the cap each re-send; the one that reaches the cap
    # does NOT re-send — the guest gets one plain final message, the reservation is
    # released, and a later text bridges normally.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    _set_stored_rejections(fake_redis, _MAX_FORM_REJECTIONS - 2)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT

    # First rejection: under the cap → re-send, counter +1.
    fake_httpx.responses.append(_flow_accepted("wamid.RS1"))
    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"}, wamid="wamid.R1")))
    assert fake_httpx.calls[-1]["json"]["interactive"]["type"] == "flow"
    assert _stored_rejections(fake_redis) == _MAX_FORM_REJECTIONS - 1

    # Second rejection: still under the cap → second re-send, now at the cap.
    fake_httpx.responses.append(_flow_accepted("wamid.RS2"))
    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"}, wamid="wamid.R2")))
    assert fake_httpx.calls[-1]["json"]["interactive"]["type"] == "flow"
    assert _stored_rejections(fake_redis) == _MAX_FORM_REJECTIONS

    # Third rejection: at the cap → NO flow send, one plain final message, released.
    fake_httpx.responses.append(_flow_accepted("wamid.FINAL"))
    with caplog.at_level("ERROR"):
        result = await handler(
            signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"}, wamid="wamid.R3"))
        )

    assert result.status_code == 200
    final = fake_httpx.calls[-1]["json"]
    assert final["type"] == "text"  # a plain message, never another flow
    assert final["text"]["body"] == _FORM_UNPROCESSABLE
    assert not await _pending_intact(fake_redis)  # released, not re-sent
    assert any(f"cap {_MAX_FORM_REJECTIONS}" in record.message for record in caplog.records)

    # A later inbound text now bridges normally — the pending is gone.
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION
    await handler(signed_request(message_payload(wamid="wamid.TXT", text="hello")))
    assert len(stub_app.conversations.accept_calls) == 1


async def test_form_rejection_re_send_failure_raises_and_keeps_pending(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A re-send that itself fails (5xx) propagates out of the webhook so Meta
    # redelivers; the pending is kept UNCHANGED (the ladder held it, no bump) and the
    # wamid is NOT marked seen, so the redelivery re-runs the ladder and re-enters.
    await _seed_pending_form()
    _seed_flow_cache(fake_redis)
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    fake_httpx.responses.append(response(500, text="graph down"))  # the re-send itself fails

    with pytest.raises(ChannelDeliveryError, match="HTTP 500"):
        await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert await _pending_intact(fake_redis)  # kept for the redelivery
    assert _stored_rejections(fake_redis) == 0  # a failed re-send does not spend the cap
    assert _SEEN_KEY not in fake_redis.store  # not marked seen — redelivery re-enters


async def test_form_rejection_cache_miss_raises_loudly(
    waba_env, handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The published-flow cache has no TTL, so a miss at re-send time means the store
    # was lost — a loud failure that re-sends nothing, keeping the pending unchanged for
    # Meta's redelivery, never a silent skip.
    await _seed_pending_form()  # no flow-cache entry seeded
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT

    with pytest.raises(AnswerForwardError, match="no published flow cached"):
        await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert await _pending_intact(fake_redis)
    assert _stored_rejections(fake_redis) == 0
    assert _SEEN_KEY not in fake_redis.store


async def test_form_reply_flow_token_mismatch_bridges_and_keeps_pending(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_pending_form(interaction_id="int-1")

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-999", "note": "x"})))

    assert result.status_code == 200
    assert not fake_httpx.calls  # not forwarded
    assert await _pending_intact(fake_redis)  # the live ask is untouched
    assert len(stub_app.conversations.accept_calls) == 1  # bridged like an uncorrelated interactive


async def test_form_reply_malformed_response_json_bridges(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    await _seed_pending_form()
    interactive = {"type": "nfm_reply", "nfm_reply": {"response_json": "{not json"}}
    payload = interactive_payload(interactive=interactive)

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert not fake_httpx.calls  # nothing forwarded
    assert await _pending_intact(fake_redis)  # the live ask is untouched
    assert len(stub_app.conversations.accept_calls) == 1  # bridged
    assert any("response_json" in record.message for record in caplog.records)


async def test_form_reply_non_object_response_json_bridges(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_pending_form()
    interactive = {"type": "nfm_reply", "nfm_reply": {"response_json": json.dumps(["not", "an", "object"])}}
    payload = interactive_payload(interactive=interactive)

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert await _pending_intact(fake_redis)
    assert len(stub_app.conversations.accept_calls) == 1


async def test_form_reply_missing_response_json_bridges(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An nfm_reply whose nfm_reply object carries no response_json string is bridged.
    await _seed_pending_form()
    interactive = {"type": "nfm_reply", "nfm_reply": {"foo": "bar"}}
    payload = interactive_payload(interactive=interactive)

    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert await _pending_intact(fake_redis)
    assert len(stub_app.conversations.accept_calls) == 1


async def test_form_reply_door_5xx_raises_and_keeps_pending(handler, channels, fake_redis: FakeRedis):
    await _seed_pending_form()
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: oops")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x"})))

    assert await _pending_intact(fake_redis)  # kept — Meta's retry re-resolves
    assert _SEEN_KEY not in fake_redis.store


async def test_unknown_change_shape_is_acked(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A change carrying neither messages nor statuses (e.g. an account update) is
    # acked with no effect.
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"id": "X", "changes": [{"field": "account_update", "value": {"event": "PARTNER_ADDED"}}]}],
    }
    result = await handler(signed_request(payload))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []
    assert stub_app.conversations.status_calls == []


async def test_empty_entry_is_acked(handler, stub_app):
    result = await handler(signed_request({"object": "whatsapp_business_account", "entry": []}))

    assert result.status_code == 200


async def test_non_object_json_payload_is_acked(handler, stub_app):
    # A well-signed body that is valid JSON but not an object (e.g. a bare list)
    # carries no entry/changes to act on — acked, no effect.
    result = await handler(signed_request([]))  # type: ignore[arg-type]

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []


# --- Interactive inbound (button/list taps) -----------------------------------


async def test_button_tap_answers_pending_select(handler, stub_app, channels, fake_redis: FakeRedis):
    await _seed_pending_select(options=["staging", "production"])
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(interactive_payload(reply_id="int-1:1", title="production")))

    assert result.status_code == 200
    # The tap resolves to options[1] under the bound ask, handed to the ladder verbatim.
    assert channels.inbound_calls[0].answer == "production"
    assert channels.inbound_calls[0].bridge.owns_retry_notice is False  # a select ask, not a form
    assert stub_app.conversations.accept_calls == []  # not bridged
    assert not await _pending_intact(fake_redis)  # released by the ladder (mirrored)
    assert _SEEN_KEY in fake_redis.store


async def test_list_reply_tap_answers_pending_select(handler, stub_app, channels, fake_redis: FakeRedis):
    await _seed_pending_select(options=["staging", "production"])
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(
        signed_request(
            interactive_payload(
                reply_type="list_reply", reply_id="int-1:0", title="staging", description="deploy target"
            )
        )
    )

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == "staging"
    assert not await _pending_intact(fake_redis)


async def test_typed_reply_to_select_ask_still_works(handler, stub_app, channels, fake_redis: FakeRedis):
    # The human may always type instead of tapping; a text reply to a select ask
    # hands its body verbatim (minus outer whitespace) to the ladder.
    await _seed_pending_select(options=["staging", "production"])
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(message_payload(text="production")))

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == "production"
    assert not await _pending_intact(fake_redis)


async def test_stale_tap_restores_pending_and_bridges_title(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A stale button from an EARLIER ask (interaction part "int-1") taps while a
    # NEWER ask ("int-2") is pending: not an answer — the pending ask survives and
    # the tap's title bridges like any unrelated message, carrying the tapped reply_id.
    await _seed_pending_select(options=["a", "b"], interaction_id="int-2")

    result = await handler(signed_request(interactive_payload(reply_id="int-1:0", title="stale choice")))

    assert result.status_code == 200
    assert not fake_httpx.calls  # no forward
    assert await _pending_intact(fake_redis)  # the newer ask is untouched
    assert stub_app.conversations.accept_calls == [
        {
            "channel": "whatsapp",
            "our_identity": PHONE_NUMBER_ID,
            "client_address": WA_ID,
            "cap_key": WA_ID,
            "text": "stale choice",
            "provider_message_id": _WAMID,
            # The bridged (non-answer) tap now carries WHICH button was tapped.
            "params": {"reply_id": "int-1:0"},
            "form": None,
            "attachments": None,
            "location": None,
        }
    ]
    assert _SEEN_KEY in fake_redis.store


async def test_tap_with_no_pending_bridges_title(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A tap with no pending question at all bridges the tap's human-readable title.
    result = await handler(signed_request(interactive_payload(reply_id="int-1:0", title="Express Option")))

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert stub_app.conversations.accept_calls[0]["text"] == "Express Option"
    assert _SEEN_KEY in fake_redis.store


async def test_malformed_tap_id_is_non_answer_marks_seen_no_5xx(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An id with no "{interaction}:{index}" shape is a non-answer: restore + bridge
    # + mark_seen, never a propagating 5xx (which would have Meta redeliver forever).
    await _seed_pending_select(options=["a", "b"])

    result = await handler(signed_request(interactive_payload(reply_id="not-an-id", title="whatever")))

    assert result.status_code == 200
    assert not fake_httpx.calls  # not forwarded
    assert await _pending_intact(fake_redis)  # restored
    assert stub_app.conversations.accept_calls[0]["text"] == "whatever"
    assert _SEEN_KEY in fake_redis.store


async def test_out_of_range_tap_index_is_non_answer_marks_seen(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An in-shape id whose index is past the ask's options is a non-answer, not a
    # silently-wrong one.
    await _seed_pending_select(options=["a", "b"])

    result = await handler(signed_request(interactive_payload(reply_id="int-1:9", title="ghost")))

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert await _pending_intact(fake_redis)
    assert stub_app.conversations.accept_calls[0]["text"] == "ghost"
    assert _SEEN_KEY in fake_redis.store


@pytest.mark.parametrize("bad_index", ["²", "1" * 5000])
async def test_unicode_or_overlong_digit_index_is_non_answer_no_5xx(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, bad_index: str
):
    # An in-shape id whose index part is isdigit()-true but int()-rejecting (a
    # Unicode superscript "²", or an absurdly-long digit string) is a non-answer:
    # bridge + mark_seen, the pending survives, and it never raises / never 5xx.
    await _seed_pending_select(options=["a", "b"], interaction_id="int-1")

    result = await handler(signed_request(interactive_payload(reply_id=f"int-1:{bad_index}", title="junk")))

    assert result.status_code == 200
    assert not fake_httpx.calls  # not forwarded
    assert await _pending_intact(fake_redis)  # the ask survives
    assert stub_app.conversations.accept_calls[0]["text"] == "junk"  # bridged
    assert _SEEN_KEY in fake_redis.store


async def test_stale_tap_does_not_pop_pending_so_a_later_genuine_reply_answers(
    handler, stub_app, channels, fake_redis: FakeRedis
):
    # A stale/non-answer tap must PEEK, not claim: the live ask survives so a
    # concurrent genuine reply from the same pair still answers (no lost answer,
    # no double-answer).
    await _seed_pending_select(options=["staging", "production"], interaction_id="int-1")
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    # A stale tap for an earlier ask — a non-answer that never reaches the ladder.
    stale = await handler(signed_request(interactive_payload(wamid="wamid.STALE", reply_id="int-0:0", title="stale")))
    assert stale.status_code == 200
    assert channels.inbound_calls == []  # the stale tap bridged, never consulted the ladder
    assert await _pending_intact(fake_redis)  # not claimed

    # The genuine reply now arrives and still resolves the still-live ask.
    genuine = await handler(
        signed_request(interactive_payload(wamid="wamid.REAL", reply_id="int-1:1", title="production"))
    )
    assert genuine.status_code == 200
    assert channels.inbound_calls[0].answer == "production"  # answered once
    assert not await _pending_intact(fake_redis)  # now consumed


async def test_malformed_interactive_object_bridges_empty_title(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A well-signed interactive message with an odd interactive object (no
    # button_reply/list_reply) is a non-answer with an empty title — acked, bridged,
    # never a 500.
    result = await handler(signed_request(interactive_payload(interactive={"type": "nope"})))

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert stub_app.conversations.accept_calls[0]["text"] == ""
    assert _SEEN_KEY in fake_redis.store


async def test_non_dict_interactive_object_bridges_empty_title(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An interactive field that is not even an object is a non-answer with an empty
    # title — acked and bridged, never a 500.
    result = await handler(signed_request(interactive_payload(interactive="not-an-object")))  # type: ignore[arg-type]

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert stub_app.conversations.accept_calls[0]["text"] == ""


async def test_odd_interactive_shape_with_pending_restores_and_bridges(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # An interactive object with no button_reply/list_reply (reply id unresolvable)
    # while a select ask is pending: not an answer — the ask is restored and the
    # (empty) title bridges.
    await _seed_pending_select(options=["a", "b"])

    result = await handler(signed_request(interactive_payload(interactive={"type": "nope"})))

    assert result.status_code == 200
    assert not fake_httpx.calls  # not forwarded
    assert await _pending_intact(fake_redis)  # restored
    assert stub_app.conversations.accept_calls[0]["text"] == ""


async def test_interactive_tap_dedupes_on_replay(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A redelivered interactive tap whose wamid is already seen short-circuits.
    fake_redis.store[_SEEN_KEY] = "1"

    result = await handler(signed_request(interactive_payload()))

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert stub_app.conversations.accept_calls == []


# --- Known-contact marker (template recipient policy) -------------------------


async def test_inbound_text_records_known_contact_marker(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await handler(signed_request(message_payload(text="hello")))

    assert _CONTACT_KEY in fake_redis.store
    assert fake_redis.ttls[_CONTACT_KEY] == 30 * 86_400  # default window, in seconds


async def test_inbound_unknown_type_records_marker_before_type_drop(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A guest who sent an unmodelled type still opened Meta's window: the marker is
    # written even though that message itself is dropped (no bridge turn, not seen).
    result = await handler(signed_request(message_payload(msg_type="system")))

    assert result.status_code == 200
    assert _CONTACT_KEY in fake_redis.store  # marker written before the type drop
    assert stub_app.conversations.accept_calls == []  # unmodelled type dropped
    assert _SEEN_KEY not in fake_redis.store


async def test_inbound_interactive_tap_records_known_contact_marker(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await handler(signed_request(interactive_payload(reply_id="int-1:0", title="x")))

    assert _CONTACT_KEY in fake_redis.store


async def test_inbound_window_zero_writes_no_marker(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_WHATSAPP_TEMPLATE_CONTACT_WINDOW_DAYS", "0")
    reset_all_settings()

    await handler(signed_request(message_payload(text="hello")))

    assert _CONTACT_KEY not in fake_redis.store  # allowlist-only mode: no tracking


# --- "Working on it" read + typing signal ------------------------------------

_TYPING_URL = f"https://graph.facebook.com/v23.0/{PHONE_NUMBER_ID}/messages"
_TYPING_BODY = {
    "messaging_product": "whatsapp",
    "status": "read",
    "message_id": _WAMID,
    "typing_indicator": {"type": "text"},
}


async def test_inbound_fires_read_typing_signal_before_branches(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A bridge (uncorrelated) text still fires the mark-as-read + typing signal:
    # the combined Graph v23.0 body to /{phone_number_id}/messages, Bearer-authed,
    # and the message still bridges.
    result = await handler(signed_request(message_payload(text="ship it")))

    assert result.status_code == 200
    assert len(fake_httpx.typing_calls) == 1
    signal = fake_httpx.typing_calls[0]
    assert signal["url"] == _TYPING_URL
    assert signal["json"] == _TYPING_BODY
    assert signal["headers"]["Authorization"].startswith("Bearer ")
    assert len(stub_app.conversations.accept_calls) == 1  # bridge still reached


async def test_correlated_question_reply_fires_typing_signal(
    handler, stub_app, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Firing at _handle_message (before the type branches) also covers a reply that
    # correlates to a pending question — the path that never reaches the bridge.
    await _seed_pending()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(message_payload(text="yes please")))

    assert result.status_code == 200
    assert [c["json"] for c in fake_httpx.typing_calls] == [_TYPING_BODY]  # typing fired
    assert channels.inbound_calls[0].answer == "yes please"  # the answer reached the ladder
    assert stub_app.conversations.accept_calls == []  # correlation hit, not the bridge


async def test_typing_signal_delivery_failure_is_logged_and_batch_survives(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A 5xx on the typing send is classified by `_send` into ChannelDeliveryError,
    # caught, and logged at WARNING — the inbound still 200-acks (never a 5xx that
    # would make Meta redeliver the whole batch) and the message still bridges.
    fake_httpx.typing_response = response(500, text="typing endpoint down")

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(message_payload(text="ship it")))

    assert result.status_code == 200
    assert len(fake_httpx.typing_calls) == 1  # the signal was attempted
    assert any("typing signal" in record.message for record in caplog.records)
    assert len(stub_app.conversations.accept_calls) == 1  # bridge still reached


# --- Ask-less form (notify) replies: the tai42-nf: token namespace ------------

# Pinned as a LITERAL (not the module constant): this prefix is wire-visible in
# every already-delivered form's token, so changing it orphans those forms.
_NF_PREFIX = "tai42-nf:"


def _nf_token(suffix: str = "cafef00d") -> str:
    _, schema_hash = build_flow(_FORM_SCHEMA)
    return f"{_NF_PREFIX}{schema_hash}:{suffix}"


def _schema_cache_key() -> str:
    _, schema_hash = build_flow(_FORM_SCHEMA)
    return f"channel:whatsapp:flow-schema:{_WABA_ID}:{schema_hash}"


def _seed_schema_cache(fake_redis: FakeRedis) -> None:
    """The durable schema entry the notify-form send left beside the flow id."""
    fake_redis.store[_schema_cache_key()] = json.dumps(_FORM_SCHEMA)


async def test_notify_form_reply_accepts_coerced_form_and_rendered_text(
    waba_env, handler, stub_app, channels, fake_redis: FakeRedis
):
    _seed_schema_cache(fake_redis)

    result = await handler(
        signed_request(
            form_reply_payload(
                {"flow_token": _nf_token(), "note": "ship it", "qty": "7", "amount": "3.5", "agree": "true"}
            )
        )
    )

    assert result.status_code == 200
    # No pending ask involved: the reply enters the conversation as a guest message,
    # never the inbound-answer ladder.
    assert channels.inbound_calls == []
    assert stub_app.conversations.accept_calls == [
        {
            "channel": "whatsapp",
            "our_identity": PHONE_NUMBER_ID,
            "client_address": WA_ID,
            "cap_key": WA_ID,
            # The rendered label:value lines every consumer sees — the boolean is JSON
            # (``true``), matching the web channel, never Python's ``True`` repr...
            "text": "note: ship it\nqty: 7\namount: 3.5\nagree: true",
            "provider_message_id": _WAMID,
            "params": None,
            # ...and the structured copy: flow_token stripped, values coerced to the
            # cached schema's types.
            "form": {"note": "ship it", "qty": 7, "amount": 3.5, "agree": True},
            "attachments": None,
            "location": None,
        }
    ]
    assert _SEEN_KEY in fake_redis.store


async def test_notify_form_reply_cache_miss_degrades_to_raw_values(
    waba_env, handler, stub_app, channels, fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    # The schema sidecar is empty (lost store): the reply DEGRADES — raw string values,
    # still accepted — never drops.
    with caplog.at_level(logging.WARNING):
        result = await handler(
            signed_request(form_reply_payload({"flow_token": _nf_token(), "note": "ship it", "qty": "7"}))
        )

    assert result.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    assert call["form"] == {"note": "ship it", "qty": "7"}  # raw: no schema to coerce against
    assert call["text"] == "note: ship it\nqty: 7"
    assert _SEEN_KEY in fake_redis.store
    assert any("forwarding raw" in record.getMessage() for record in caplog.records)


async def test_notify_form_reply_unset_waba_id_degrades_to_raw_values(
    handler, stub_app, channels, fake_redis: FakeRedis
):
    # No CHANNEL_WHATSAPP_WABA_ID: the cache cannot even be addressed — same degrade,
    # never a raise that would have Meta redeliver a permanently-failing reply.
    result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token(), "qty": "7"})))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls[0]["form"] == {"qty": "7"}
    assert _SEEN_KEY in fake_redis.store


async def test_notify_form_reply_leaves_pending_ask_untouched(
    waba_env, handler, stub_app, channels, fake_redis: FakeRedis
):
    # The masquerade fence: a notify-form reply on a pair with a PENDING ask must not
    # answer, claim, or disturb that ask — the token namespace routes it out before any
    # pending peek.
    await _seed_pending_form()
    _seed_schema_cache(fake_redis)
    pending_before = fake_redis.store[_PENDING_KEY]

    result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token(), "note": "unrelated"})))

    assert result.status_code == 200
    assert channels.inbound_calls == []  # the ladder never saw it
    assert fake_redis.store[_PENDING_KEY] == pending_before  # byte-identical record
    assert stub_app.conversations.accept_calls[0]["form"] == {"note": "unrelated"}

    # The ask is still answerable: a genuine (non-prefixed) form reply resolves it.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "the answer"}, wamid="wamid.ASK")))
    assert channels.inbound_calls[0].answer == {"note": "the answer"}


async def test_notify_form_reply_redelivery_deduped(waba_env, handler, stub_app, channels, fake_redis: FakeRedis):
    _seed_schema_cache(fake_redis)
    fake_redis.store[_SEEN_KEY] = "1"

    result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token(), "note": "again"})))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []
    assert channels.inbound_calls == []


async def test_notify_form_reply_empty_form_bridges_compact_json(waba_env, handler, stub_app, fake_redis: FakeRedis):
    # A completed form carrying nothing but its token still needs non-blank text for
    # the accept door: the compact-JSON fallback.
    _seed_schema_cache(fake_redis)

    result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token()})))

    assert result.status_code == 200
    call = stub_app.conversations.accept_calls[0]
    assert call["form"] == {}
    assert call["text"] == "{}"
    assert _SEEN_KEY in fake_redis.store


async def test_notify_form_reply_unrouted_logged_and_acked(
    waba_env, handler, stub_app, fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    _seed_schema_cache(fake_redis)
    stub_app.conversations.accept_error = LookupError("no route for (whatsapp, ...)")

    with caplog.at_level(logging.WARNING):
        result = await handler(signed_request(form_reply_payload({"flow_token": _nf_token(), "note": "x"})))

    assert result.status_code == 200  # dropped and logged, never a 5xx retry loop
    assert _SEEN_KEY in fake_redis.store
    assert any("unrouted" in record.getMessage() for record in caplog.records)


async def test_non_prefixed_form_reply_takes_the_ask_path_unchanged(
    waba_env, handler, stub_app, channels, fake_redis: FakeRedis
):
    # Regression pin: a reply whose token is NOT in the notify namespace runs the ask
    # path byte-identically — pending peek, ladder resolve, no direct bridge accept.
    await _seed_pending_form()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(form_reply_payload({"flow_token": "int-1", "note": "x", "qty": "7"})))

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == {"note": "x", "qty": 7}
    assert stub_app.conversations.accept_calls == []


# --- Inbound entry-params vocabulary (bridge-path only) ------------------------
#
# The channel-side lane of the channels-vocabulary wave. Every param below rides ONLY
# on the conversation-bridge path (a fresh turn via ``conversations.accept``); the
# correlated-answer path forwards ``{"answer": …}`` to the callback door — a seam that
# carries no params — so a tap/button that ANSWERS a pending question surfaces none.


def _params_envelope(message: dict, *, phone_number_id: str = PHONE_NUMBER_ID) -> dict:
    """A signed-inbound envelope carrying one arbitrary message object — for the message
    shapes (``button`` type, ``referral``, ``context``, ``errors``) the shared
    ``message_payload``/``interactive_payload`` helpers do not build."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"display_phone_number": "15551112222", "phone_number_id": phone_number_id},
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


async def test_button_reply_tap_bridges_reply_id_in_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Claim 1 (born-red): a button_reply tap with NO pending question bridges the tap's
    # title AND carries the tapped wire id under params.reply_id. On the OLD code the
    # bridge accept passed no params, so params was None.
    result = await handler(signed_request(interactive_payload(reply_id="int-9:2", title="Talk to sales")))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Talk to sales"
    assert call["params"] == {"reply_id": "int-9:2"}
    assert _SEEN_KEY in fake_redis.store


async def test_list_reply_tap_bridges_reply_id_and_description(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Claim 1 (born-red): a list pick with a description carries both reply_id and
    # reply_description. OLD code: params None (and reply_description never extracted).
    result = await handler(
        signed_request(
            interactive_payload(
                reply_type="list_reply", reply_id="int-3:0", title="Standard", description="3-5 business days"
            )
        )
    )

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Standard"
    assert call["params"] == {"reply_id": "int-3:0", "reply_description": "3-5 business days"}


async def test_button_message_type_bridges_text_and_payload(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Claim 2 (born-red): a template quick-reply tap arrives as a ``button`` message
    # (fields button.text + button.payload). OLD code dropped it in the else-branch, so
    # accept was NEVER called; NEW code bridges the text with params.button_payload.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "button",
        "button": {"text": "Confirm appointment", "payload": "CONFIRM_APPT_42"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Confirm appointment"
    assert call["params"] == {"button_payload": "CONFIRM_APPT_42"}
    assert _SEEN_KEY in fake_redis.store


async def test_button_message_type_answers_pending_like_text(
    handler, stub_app, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A ``button`` quick-reply while a question is pending answers with its visible text,
    # mirroring a typed reply — the ask path, no bridge, no params seam.
    await _seed_pending_select(options=["Confirm", "Cancel"])
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "button",
        "button": {"text": "Confirm", "payload": "CONFIRM_APPT_42"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    assert channels.inbound_calls[0].answer == "Confirm"  # answered as a typed reply would
    assert stub_app.conversations.accept_calls == []  # the ask path, not the bridge


async def test_referral_fields_carried_as_params(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Claim 3 (born-red): a click-to-WhatsApp / QR referral forwards its fields as opaque,
    # prefixed params on the bridged turn. OLD code: params None.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "text",
        "text": {"body": "hi from the ad"},
        "referral": {
            "source_url": "https://fb.me/ad/123",
            "source_id": "ad-123",
            "source_type": "ad",
            "ctwa_clid": "clid-abc",
            "headline": "50% off today",
            "body": "Tap to chat",
        },
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "hi from the ad"
    assert call["params"] == {
        "referral_source_url": "https://fb.me/ad/123",
        "referral_source_id": "ad-123",
        "referral_source_type": "ad",
        "referral_ctwa_clid": "clid-abc",
        "referral_headline": "50% off today",
        "referral_body": "Tap to chat",
    }


async def test_reply_context_carried_as_params(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # Claim 4 (born-red): a message quoting an earlier one carries context.id as
    # params.context_message_id. OLD code: params None.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "text",
        "text": {"body": "re: that"},
        "context": {"from": "15551112222", "id": "wamid.QUOTED"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "re: that"
    assert call["params"] == {"context_message_id": "wamid.QUOTED"}


async def test_referral_and_context_merge_with_reply_id_on_a_tap(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # The message-level referral/context params merge with an interactive tap's reply_id
    # on one bridged turn (the union of both key spaces).
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "interactive",
        "interactive": {"type": "button_reply", "button_reply": {"id": "int-1:0", "title": "Yes"}},
        "context": {"id": "wamid.QUOTED"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["params"] == {"reply_id": "int-1:0", "context_message_id": "wamid.QUOTED"}


async def test_inbound_error_notice_logged_warning_not_bridged(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # Claim 5 (born-red): a Meta inbound error notice (an unsupported message type the
    # guest sent) is logged at WARNING with the detail and NOT bridged. OLD code dropped
    # it in the else-branch at DEBUG with no type/detail, so no WARNING was emitted.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "unsupported",
        "errors": [{"code": 131051, "title": "Unsupported message type"}],
    }
    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []  # never bridged
    assert any("error notice" in record.message and "131051" in record.getMessage() for record in caplog.records)


async def test_unhandled_message_type_logged_at_info_naming_the_type(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A type the channel does not model (media/location/contacts/reactions now bridge) is
    # dropped, and the drop log names the type at INFO so an operator sees WHAT was dropped.
    with caplog.at_level("INFO"):
        result = await handler(signed_request(message_payload(msg_type="system")))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []
    assert any(
        record.levelname == "INFO" and "system" in record.getMessage() and "unhandled" in record.getMessage()
        for record in caplog.records
    )


async def test_oversized_param_value_is_dropped_not_5xx(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A referral field over the contract's per-value cap is dropped at extraction (never
    # truncated, never a 5xx), while the well-formed sibling fields still ride.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "text",
        "text": {"body": "hi"},
        "referral": {"source_id": "ad-1", "headline": "x" * 600},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["params"] == {"referral_source_id": "ad-1"}  # the 600-char headline dropped


# --- Inbound media / location / contacts / reactions (the "everything found" set) ---------


async def test_inbound_image_with_caption_bridges_caption_as_text_and_media_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A guest photo with a caption: the caption is the turn text; the media's identity rides
    # params (media_kind/id/mime/sha256). No typed attachment (see the INBOUND MEDIA design
    # note) — the id is the re-fetch handle.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "image",
        "image": {"id": "media-abc", "mime_type": "image/jpeg", "sha256": "deadbeef", "caption": "the broken part"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "the broken part"
    assert call["attachments"] is None  # design gap: no served-media ingestion seam
    assert call["params"] == {
        "media_kind": "image",
        "media_id": "media-abc",
        "media_mime_type": "image/jpeg",
        "media_sha256": "deadbeef",
    }
    assert _SEEN_KEY in fake_redis.store


async def test_inbound_image_without_caption_uses_placeholder_text(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "mime_type": "image/png"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[image]"  # accept refuses blank text — a faithful placeholder rides
    assert call["params"] == {"media_kind": "image", "media_id": "m1", "media_mime_type": "image/png"}


async def test_inbound_document_carries_filename_in_text_and_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "document",
        "document": {"id": "doc-1", "mime_type": "application/pdf", "filename": "invoice.pdf"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[document: invoice.pdf]"
    assert call["params"]["media_filename"] == "invoice.pdf"
    assert call["params"]["media_kind"] == "document"


async def test_inbound_voice_note_flags_voice_param_and_placeholder(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "audio",
        "audio": {"id": "a1", "mime_type": "audio/ogg", "voice": True},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[voice message]"
    assert call["params"]["media_voice"] == "true"


async def test_inbound_animated_sticker_flags_animated_param(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "sticker",
        "sticker": {"id": "s1", "mime_type": "image/webp", "animated": True},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[sticker]"
    assert call["params"]["sticker_animated"] == "true"
    assert call["params"]["media_kind"] == "sticker"


async def test_inbound_video_bridges_and_dedupes_on_replay(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {"id": _WAMID, "from": WA_ID, "type": "video", "video": {"id": "v1", "mime_type": "video/mp4"}}
    await handler(signed_request(_params_envelope(message)))
    # A redelivery of the same wamid is short-circuited (already seen).
    await handler(signed_request(_params_envelope(message)))

    assert len(stub_app.conversations.accept_calls) == 1
    assert stub_app.conversations.accept_calls[0]["params"]["media_kind"] == "video"


async def test_inbound_media_caption_carries_reply_context_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Message-level context (reply-to) merges with the media params on the bridged turn.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "image",
        "image": {"id": "m1", "mime_type": "image/jpeg", "caption": "see this"},
        "context": {"id": "wamid.QUOTED"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "see this"
    assert call["params"]["context_message_id"] == "wamid.QUOTED"
    assert call["params"]["media_id"] == "m1"


async def test_inbound_media_while_ask_pending_bridges_leaving_ask_parked(
    handler, stub_app, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A photo cannot answer a pending text/select/form ask — it bridges as a fresh turn and
    # the parked ask is left untouched (never reaches the answer ladder).
    await _seed_pending()
    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "caption": "unrelated photo"}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    assert channels.inbound_calls == []  # never the answer ladder
    assert stub_app.conversations.accept_calls[0]["text"] == "unrelated photo"
    assert _PENDING_KEY in fake_redis.store  # the ask stays parked


async def test_inbound_location_lands_typed_location_on_accept(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A shared location lands as a typed LocationElement on accept (a machine-consumable
    # field, not a param); the turn text is the place name.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "location",
        "location": {"latitude": 51.5, "longitude": -0.12, "name": "Office", "address": "1 High St"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Office"
    assert call["location"] == LocationElement(latitude=51.5, longitude=-0.12, name="Office", address="1 High St")
    assert call["params"] is None


async def test_inbound_location_without_labels_uses_coordinate_text(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {"id": _WAMID, "from": WA_ID, "type": "location", "location": {"latitude": 1.5, "longitude": 2.5}}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "location: 1.5, 2.5"
    assert call["location"] == LocationElement(latitude=1.5, longitude=2.5)


async def test_inbound_location_out_of_range_degrades_to_text_only(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # An out-of-range latitude cannot build a LocationElement — the turn still bridges as
    # text (never lost), with no typed location.
    message = {"id": _WAMID, "from": WA_ID, "type": "location", "location": {"latitude": 999.0, "longitude": 2.5}}
    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["location"] is None
    assert call["text"] == "location: 999.0, 2.5"


async def test_inbound_contacts_bridge_names_as_text_and_cards_in_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    contacts = [
        {"name": {"formatted_name": "Jane Doe"}, "phones": [{"phone": "+15551230001"}]},
        {"name": {"formatted_name": "John Roe"}},
    ]
    message = {"id": _WAMID, "from": WA_ID, "type": "contacts", "contacts": contacts}
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "Jane Doe, John Roe"
    assert call["params"]["contacts_count"] == "2"
    assert json.loads(call["params"]["contacts"]) == contacts


async def test_inbound_reaction_carries_emoji_and_target_params(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "reaction",
        "reaction": {"message_id": "wamid.TARGET", "emoji": "👍"},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "👍"
    assert call["params"] == {"reaction_emoji": "👍", "reaction_message_id": "wamid.TARGET"}


async def test_inbound_removed_reaction_has_placeholder_text_and_no_emoji_param(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "reaction",
        "reaction": {"message_id": "wamid.TARGET", "emoji": ""},
    }
    result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    (call,) = stub_app.conversations.accept_calls
    assert call["text"] == "[reaction removed]"
    assert call["params"] == {"reaction_message_id": "wamid.TARGET"}  # empty emoji dropped


async def test_inbound_media_records_known_contact_marker(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    message = {"id": _WAMID, "from": WA_ID, "type": "image", "image": {"id": "m1", "caption": "hi"}}
    await handler(signed_request(_params_envelope(message)))

    assert _CONTACT_KEY in fake_redis.store  # a guest photo opens Meta's window too
