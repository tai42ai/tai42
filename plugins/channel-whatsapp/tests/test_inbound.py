"""The inbound webhook handler — GET verification, signature validation, dedupe,
correlation, bridge branch, forward policy, and delivery-status webhooks."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx
import pytest
from starlette.responses import Response
from tai42_contract.conversations import DeliveryReceipt

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_whatsapp.correlation import reserve_pending
from tai42_channel_whatsapp.inbound import AnswerForwardError
from tests.conftest import (
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

_PATH = "/api/channels/whatsapp/inbound"
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
    assert route.authed is False
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


async def test_pending_question_resolves_before_bridge(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(message_payload(text="yes please")))

    assert result.status_code == 200
    assert fake_httpx.calls[0]["url"] == _CALLBACK
    assert fake_httpx.calls[0]["json"] == {"answer": "yes please"}
    assert stub_app.conversations.accept_calls == []  # the bridge was NOT reached
    assert not await _pending_intact(fake_redis)  # consumed
    assert _SEEN_KEY in fake_redis.store


async def test_answer_is_body_verbatim_minus_outer_whitespace(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    await handler(signed_request(message_payload(text="  yes please \n")))

    assert fake_httpx.calls[0]["json"] == {"answer": "yes please"}


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
            "text": "ship it",
            "provider_message_id": _WAMID,
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
            "text": "late reply",
            "provider_message_id": _WAMID,
        }
    ]


async def test_non_text_message_acked_no_turn(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level("DEBUG"):
        result = await handler(signed_request(message_payload(msg_type="image")))

    assert result.status_code == 200
    assert stub_app.conversations.accept_calls == []  # no bridge turn
    assert not fake_httpx.calls
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
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
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
    fake_httpx.responses.append(response(500, text="down"))

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(payload))

    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M2"]  # m2 committed
    assert await _pending_intact(fake_redis)  # m1 restored — it retries on redelivery
    assert "channel:whatsapp:seen:wamid.M1" not in fake_redis.store  # m1 un-acked
    assert "channel:whatsapp:seen:wamid.M2" in fake_redis.store  # m2 acked

    # Redelivery: m1 fails again (still 5xx), m2 is dedupe-skipped (no re-bridge).
    fake_httpx.responses.append(response(500, text="down"))
    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(payload))

    assert [c["provider_message_id"] for c in stub_app.conversations.accept_calls] == ["wamid.M2"]  # still once
    assert len(fake_httpx.calls) == 2  # m1 forwarded twice; m2 never forwarded


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


async def test_door_5xx_restores_pending_and_raises_so_meta_retries(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await _seed_pending()
    fake_httpx.responses.append(response(500, text="oops"))

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(message_payload()))

    assert await _pending_intact(fake_redis)  # restored
    assert _SEEN_KEY not in fake_redis.store  # NOT marked seen — the retry must not dedupe away

    fake_httpx.responses.append(response(200))
    retry = await handler(signed_request(message_payload()))
    assert retry.status_code == 200
    assert len(fake_httpx.calls) == 2
    assert not await _pending_intact(fake_redis)


async def test_door_404_is_terminal_drop(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    await _seed_pending()
    fake_httpx.responses.append(response(404))

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(message_payload()))

    assert result.status_code == 200
    assert not await _pending_intact(fake_redis)  # correlation stays dropped
    assert _SEEN_KEY in fake_redis.store
    assert any("terminal HTTP 404" in record.message for record in caplog.records)

    redelivery = await handler(signed_request(message_payload()))
    assert redelivery.status_code == 200
    assert len(fake_httpx.calls) == 1  # dedupe — no retry storm on a dead ticket


async def test_door_400_keeps_correlation_for_a_re_reply(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    await _seed_pending()
    fake_httpx.responses.append(response(400))

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(message_payload()))

    assert result.status_code == 200
    assert await _pending_intact(fake_redis)  # restored — the human can reply again
    assert _SEEN_KEY in fake_redis.store
    assert any("rejected the answer" in record.message for record in caplog.records)

    # A NEW reply from the pair (new wamid) pops it and forwards.
    fake_httpx.responses.append(response(200))
    retry = await handler(signed_request(message_payload(wamid="wamid.SECOND", text="option two")))
    assert retry.status_code == 200
    assert fake_httpx.calls[-1]["json"] == {"answer": "option two"}
    assert not await _pending_intact(fake_redis)


async def test_forward_transport_failure_restores_and_propagates(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(httpx.ConnectError("door unreachable"))

    with pytest.raises(httpx.ConnectError):
        await handler(signed_request(message_payload()))

    assert await _pending_intact(fake_redis)  # restored
    assert _SEEN_KEY not in fake_redis.store  # not marked seen


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


async def test_button_tap_answers_pending_select(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending_select(options=["staging", "production"])
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(interactive_payload(reply_id="int-1:1", title="production")))

    assert result.status_code == 200
    # The tap resolves to options[1] under the bound ask, forwarded verbatim.
    assert fake_httpx.calls[0]["url"] == _CALLBACK
    assert fake_httpx.calls[0]["json"] == {"answer": "production"}
    assert stub_app.conversations.accept_calls == []  # not bridged
    assert not await _pending_intact(fake_redis)  # consumed
    assert _SEEN_KEY in fake_redis.store


async def test_list_reply_tap_answers_pending_select(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending_select(options=["staging", "production"])
    fake_httpx.responses.append(response(200))

    result = await handler(
        signed_request(
            interactive_payload(
                reply_type="list_reply", reply_id="int-1:0", title="staging", description="deploy target"
            )
        )
    )

    assert result.status_code == 200
    assert fake_httpx.calls[0]["json"] == {"answer": "staging"}
    assert not await _pending_intact(fake_redis)


async def test_typed_reply_to_select_ask_still_works(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The human may always type instead of tapping; a text reply to a select ask
    # forwards its body verbatim (minus outer whitespace).
    await _seed_pending_select(options=["staging", "production"])
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(message_payload(text="production")))

    assert result.status_code == 200
    assert fake_httpx.calls[0]["json"] == {"answer": "production"}
    assert not await _pending_intact(fake_redis)


async def test_stale_tap_restores_pending_and_bridges_title(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A stale button from an EARLIER ask (interaction part "int-1") taps while a
    # NEWER ask ("int-2") is pending: not an answer — the pending ask survives and
    # the tap's title bridges like any unrelated message.
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
            "text": "stale choice",
            "provider_message_id": _WAMID,
        }
    ]
    assert _SEEN_KEY in fake_redis.store


async def test_tap_with_no_pending_bridges_title(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A tap with no pending question at all bridges the tap's human-readable title.
    result = await handler(signed_request(interactive_payload(reply_id="int-1:0", title="Priority Mail")))

    assert result.status_code == 200
    assert not fake_httpx.calls
    assert stub_app.conversations.accept_calls[0]["text"] == "Priority Mail"
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
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A stale/non-answer tap must PEEK, not POP: the live ask survives so a
    # concurrent genuine reply from the same pair still answers (no lost answer,
    # no double-answer).
    await _seed_pending_select(options=["staging", "production"], interaction_id="int-1")

    # A stale tap for an earlier ask — a non-answer that must NOT claim the pending.
    stale = await handler(signed_request(interactive_payload(wamid="wamid.STALE", reply_id="int-0:0", title="stale")))
    assert stale.status_code == 200
    assert await _pending_intact(fake_redis)  # not popped

    # The genuine reply now arrives and still resolves the still-live ask.
    fake_httpx.responses.append(response(200))
    genuine = await handler(
        signed_request(interactive_payload(wamid="wamid.REAL", reply_id="int-1:1", title="production"))
    )
    assert genuine.status_code == 200
    assert fake_httpx.calls[0]["json"] == {"answer": "production"}  # answered once
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


async def test_inbound_non_text_records_marker_before_type_drop(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A guest who sent only a photo still opened Meta's window: the marker is
    # written even though the image itself is dropped (no bridge turn, not seen).
    result = await handler(signed_request(message_payload(msg_type="image")))

    assert result.status_code == 200
    assert _CONTACT_KEY in fake_redis.store  # marker written before the type drop
    assert stub_app.conversations.accept_calls == []  # image still dropped
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
