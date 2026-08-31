"""The inbound webhook handler — signature validation, dedupe, correlation, forward policy."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import pytest
from starlette.responses import Response
from tai42_contract.channels import AnswerForwardError, InboundAnswerOutcome
from tai42_contract.conversations import BlankInboundTextError, DeliveryReceipt

import tai42_channel_twilio.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_twilio.correlation import reserve_pending

from .conftest import (
    FakeHttpx,
    FakeRedis,
    build_request,
    compute_signature,
    make_delivery,
    response,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("twilio_env")

# The absolute URL Twilio delivers to and signs over (the mount base resolves to
# this at the default). The registered route is the relative ``/inbound`` beneath it.
_PATH = "/api/channels/twilio/inbound"
_ROUTE = "/inbound"
_TWILIO = "+15550000001"
_HUMAN = "+15550000002"
_CALLBACK = "https://app.example/api/interactions/callback/ticket-1"
_SEEN_KEY = "channel:twilio:seen:SM777"


def _pairs(**overrides: str) -> list[tuple[str, str]]:
    form = {"MessageSid": "SM777", "To": _TWILIO, "From": _HUMAN, "Body": "yes please"}
    form.update(overrides)
    return list(form.items())


@pytest.fixture
def handler(stub_app) -> Callable[..., Awaitable[Response]]:
    routes = [route for route in stub_app.http.routes if route.path == _ROUTE]
    assert len(routes) == 1
    route = routes[0]
    assert route.methods == ["POST"]
    assert route.authed is None
    return route.handler


async def _seed_pending(callback_url: str = _CALLBACK) -> None:
    delivery = make_delivery(callback_url=callback_url)
    await reserve_pending(_TWILIO, _HUMAN, delivery.callback_url, delivery.interaction_id, delivery.timeout_at)


async def _pending_intact(fake_redis: FakeRedis) -> bool:
    return f"channel:twilio:pending:{_TWILIO}:{_HUMAN}" in fake_redis.store


# --- Happy path ---------------------------------------------------------------


async def test_valid_signature_hands_reply_to_shared_ladder(handler, channels, fake_redis: FakeRedis):
    # The signed reply is handed to the ONE shared ladder with the number-pair key,
    # the Body as the answer, and the bridge context; a FORWARDED outcome acks 204
    # and marks the sid seen. The plugin no longer forwards itself — the ladder does.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert len(channels.inbound_calls) == 1
    call = channels.inbound_calls[0]
    assert call.channel_id == "twilio"
    assert call.correlation_key == f"{_TWILIO}:{_HUMAN}"
    assert call.answer == "yes please"
    assert call.bridge.channel_id == "twilio"
    assert call.bridge.our_identity == _TWILIO
    assert call.bridge.client_address == _HUMAN
    assert call.bridge.cap_key == _HUMAN
    assert call.bridge.provider_message_id == "SM777"
    assert call.bridge.bridge_text == "yes please"
    assert _SEEN_KEY in fake_redis.store  # sid marked seen on a resolved outcome


async def test_answer_is_body_verbatim_minus_outer_whitespace(handler, channels, fake_redis: FakeRedis):
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    await handler(signed_request(_pairs(Body=" yes please \n")))
    # The answer forwarded to the ladder is the Body minus outer whitespace...
    assert channels.inbound_calls[0].answer == "yes please"
    # ...while the bridge text a gone-ask fallback would use is the Body verbatim.
    assert channels.inbound_calls[0].bridge.bridge_text == " yes please \n"


async def test_public_url_reconstructed_from_forwarded_headers(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # The ASGI scope says http://app-internal:8000, the forwarded headers say
    # https://public.example — the signature over the PUBLIC url must validate.
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(_pairs(), proto="https", host="public.example"))

    assert result.status_code == 204


async def test_duplicate_form_keys_validate(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    # A dict-collapsing implementation would drop one MediaUrl0 pair and 401 here.
    await _seed_pending()
    fake_httpx.responses.append(response(200))
    pairs = [*_pairs(), ("MediaUrl0", "https://a.example/1"), ("MediaUrl0", "https://b.example/2")]

    result = await handler(signed_request(pairs))

    assert result.status_code == 204


async def test_query_string_is_part_of_the_signed_url(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    result = await handler(signed_request(_pairs(), query="x=1"))
    assert result.status_code == 204

    # The same request signed WITHOUT the query must be rejected (a signature failure
    # short-circuits before the store, so no fresh reservation is needed here).
    rejected = await handler(signed_request(_pairs(), query="x=1", sign_url=f"https://public.example{_PATH}"))
    assert rejected.status_code == 401


async def test_fallback_to_request_scheme_and_host_without_proxy_headers(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # No X-Forwarded-* headers: the signed URL is the request's own scheme + Host.
    await _seed_pending()
    fake_httpx.responses.append(response(200))

    def strip_forwarded(headers: dict[str, str]) -> None:
        del headers["x-forwarded-proto"]
        del headers["x-forwarded-host"]

    result = await handler(
        signed_request(_pairs(), sign_url=f"http://app-internal:8000{_PATH}", tamper=strip_forwarded)
    )

    assert result.status_code == 204


# --- Fail-closed branches -----------------------------------------------------


async def _assert_rejected(handler, request, fake_redis: FakeRedis, fake_httpx: FakeHttpx, status: int = 401):
    result = await handler(request)
    assert result.status_code == status
    assert not fake_httpx.calls  # zero forwards
    assert await _pending_intact(fake_redis)  # pending NOT consumed


async def test_missing_signature_header_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    await _assert_rejected(handler, signed_request(_pairs(), omit_signature=True), fake_redis, fake_httpx)


async def test_wrong_token_signature_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    await _assert_rejected(handler, signed_request(_pairs(), token="other-token"), fake_redis, fake_httpx)


async def test_url_mismatch_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    request = signed_request(_pairs(), sign_url=f"https://evil.example{_PATH}")
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_non_base64_signature_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    request = signed_request(_pairs(), signature="!!! not base64 !!!")
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_wrong_digest_length_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    request = signed_request(_pairs(), signature="c2hvcnQ=")  # base64("short") — 5 bytes, not 20
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_empty_auth_token_fails_closed_with_config_error(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    await _seed_pending()
    monkeypatch.setenv("CHANNEL_TWILIO_AUTH_TOKEN", "")
    reset_all_settings()

    # Operator misconfiguration is a logged, constant 500 — never a 401 that
    # reads like an ordinary bad signature, never any processing.
    result = await handler(signed_request(_pairs()))

    assert result.status_code == 500
    assert json.loads(result.body) == {"error": "channel misconfigured"}
    assert not fake_httpx.calls
    assert await _pending_intact(fake_redis)


async def test_tampered_body_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    # Sign one body, deliver another.
    signature = compute_signature("testtoken", f"https://public.example{_PATH}", _pairs())
    request = signed_request(_pairs(Body="no way"), signature=signature)
    await _assert_rejected(handler, request, fake_redis, fake_httpx)


async def test_missing_host_header_rejected(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()

    def strip_hosts(headers: dict[str, str]) -> None:
        del headers["x-forwarded-host"]
        del headers["host"]

    await _assert_rejected(handler, signed_request(_pairs(), tamper=strip_hosts), fake_redis, fake_httpx)


async def test_oversized_body_413_before_any_signature_work(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    # No X-Twilio-Signature header at all, yet the response is 413 (not 401):
    # the bounded read runs BEFORE any HMAC work.
    request = build_request(
        body=b"",
        chunks=[b"x" * (512 * 1024), b"y" * (512 * 1024), b"z"],
        headers={"host": "app-internal:8000", "content-type": "application/x-www-form-urlencoded"},
    )
    await _assert_rejected(handler, request, fake_redis, fake_httpx, status=413)


async def test_non_utf8_body_is_clean_401(
    handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    await _seed_pending()
    request = build_request(
        body=b"\xff\xfe",
        headers={
            "host": "app-internal:8000",
            "content-type": "application/x-www-form-urlencoded",
            "x-twilio-signature": "AAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        },
    )

    with caplog.at_level("WARNING"):
        await _assert_rejected(handler, request, fake_redis, fake_httpx)

    assert any("not valid UTF-8" in record.message for record in caplog.records)


# --- Post-signature branches ---------------------------------------------------


async def test_missing_message_sid_is_400(handler, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await _seed_pending()
    pairs = [pair for pair in _pairs() if pair[0] != "MessageSid"]
    await _assert_rejected(handler, signed_request(pairs), fake_redis, fake_httpx, status=400)


async def test_message_sid_dedupe_skips_second_delivery(handler, channels, fake_redis: FakeRedis):
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    first = await handler(signed_request(_pairs()))
    second = await handler(signed_request(_pairs()))

    assert first.status_code == 204
    assert second.status_code == 204
    # The second delivery is deduped on its MessageSid BEFORE the ladder — the shared
    # handler is consulted exactly once.
    assert len(channels.inbound_calls) == 1


async def test_uncorrelated_unrouted_inbound_logged_ack_no_turn(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # No pending question and no bound route: accept() raises LookupError -> the
    # bridge logs and success-acks, marks the sid seen, and starts no forward.
    stub_app.conversations.accept_error = LookupError("no channel conversation route matches (twilio, +15550000001)")

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert not fake_httpx.calls  # no ask_user forward
    assert len(stub_app.conversations.accept_calls) == 1  # the bridge was attempted
    assert any("unrouted" in record.message for record in caplog.records)
    assert _SEEN_KEY in fake_redis.store  # replay of the same sid dedupes


async def test_uncorrelated_blank_inbound_logged_ack_no_turn(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # No pending question and a whitespace-only body: accept() raises
    # BlankInboundTextError -> the bridge logs and success-acks, marks the sid seen,
    # and starts no forward (never a 5xx retry-storm), no turn produced.
    stub_app.conversations.accept_error = BlankInboundTextError("inbound text is blank")

    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_pairs(Body="   ")))

    assert result.status_code == 204
    assert not fake_httpx.calls  # no ask_user forward
    assert len(stub_app.conversations.accept_calls) == 1  # the bridge was attempted, no turn produced
    assert any("blank" in record.message for record in caplog.records)
    assert _SEEN_KEY in fake_redis.store  # replay of the same sid dedupes


# --- Bridge branch (correlation miss) -----------------------------------------


async def test_uncorrelated_routed_inbound_calls_accept_with_verbatim_args(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # No pending question, a route is bound: accept() is called with To/From/Body
    # verbatim and the MessageSid, the sid is marked seen, and nothing is forwarded.
    result = await handler(signed_request(_pairs(Body="ship it")))

    assert result.status_code == 204
    assert not fake_httpx.calls  # bridge does not use the ask_user forward
    assert stub_app.conversations.accept_calls == [
        {
            "channel": "twilio",
            "our_identity": _TWILIO,
            "client_address": _HUMAN,
            # The provider attests the From number, so it is also the accountable cap key.
            "cap_key": _HUMAN,
            "text": "ship it",
            "provider_message_id": "SM777",
        }
    ]
    assert _SEEN_KEY in fake_redis.store


async def test_pending_question_resolves_before_bridge(handler, stub_app, channels, fake_redis: FakeRedis):
    # a correlated pending question resolves the ask via the ladder and never reaches
    # the caller's fresh-turn bridge.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert len(channels.inbound_calls) == 1
    assert stub_app.conversations.accept_calls == []  # the caller's bridge was NOT reached


async def test_signature_failure_short_circuits_before_bridge(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # Regression: a bad signature is rejected before any correlation or bridge code.
    result = await handler(signed_request(_pairs(), token="other-token"))

    assert result.status_code == 401
    assert stub_app.conversations.accept_calls == []
    assert _SEEN_KEY not in fake_redis.store


async def test_dedupe_hit_short_circuits_before_bridge(handler, stub_app, channels, fake_redis: FakeRedis):
    # Regression: an already-seen MessageSid short-circuits before the ladder and bridge.
    fake_redis.store[_SEEN_KEY] = "1"

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert channels.inbound_calls == []  # the ladder was never consulted
    assert stub_app.conversations.accept_calls == []


async def test_bridge_overflow_propagates_and_does_not_dedupe(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A retryable/infrastructure failure from accept() (not a LookupError) propagates
    # as a 5xx so Twilio redelivers; the sid is NOT marked seen.
    stub_app.conversations.accept_error = RuntimeError("per-thread FIFO is full")

    with pytest.raises(RuntimeError, match="FIFO is full"):
        await handler(signed_request(_pairs()))

    assert _SEEN_KEY not in fake_redis.store


async def test_ladder_forward_error_raises_and_does_not_dedupe(handler, channels, fake_redis: FakeRedis):
    # A door 5xx / 401 / transport fault surfaces as AnswerForwardError from the ladder
    # (which keeps the correlation for the retry); the plugin lets it propagate and does
    # NOT mark the sid seen, so Twilio's redelivery re-runs the ladder.
    channels.inbound_error = AnswerForwardError("interactions callback rejected the answer: HTTP 500: oops")

    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await handler(signed_request(_pairs()))

    assert _SEEN_KEY not in fake_redis.store  # NOT marked seen — the retry must not dedupe away


async def test_ladder_bridged_outcome_acks_and_marks_seen(handler, channels, fake_redis: FakeRedis):
    # A BRIDGED outcome (the ask is gone / a hard mismatch — the ladder already bridged
    # the reply internally, carrying the Body verbatim under the MessageSid dedupe key)
    # acks 204 and marks the sid seen so a redelivery is not re-processed.
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED

    result = await handler(signed_request(_pairs(Body="ship it")))

    assert result.status_code == 204
    assert len(channels.inbound_calls) == 1
    assert channels.inbound_calls[0].bridge.bridge_text == "ship it"
    assert channels.inbound_calls[0].bridge.provider_message_id == "SM777"
    assert _SEEN_KEY in fake_redis.store

    # Twilio redelivers the same MessageSid: dedupe short-circuits — the ladder is not
    # consulted a second time.
    redelivery = await handler(signed_request(_pairs(Body="ship it")))
    assert redelivery.status_code == 204
    assert len(channels.inbound_calls) == 1  # still once


async def test_ladder_retry_kept_outcome_acks_and_marks_seen(handler, channels, fake_redis: FakeRedis):
    # A RETRY_KEPT outcome (the door rejected a re-answerable ask; the ladder kept the
    # correlation and told the guest what's expected) acks 204 and marks the sid seen —
    # redelivering the same body would be rejected again.
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert len(channels.inbound_calls) == 1
    assert _SEEN_KEY in fake_redis.store


# --- Signed delivery-status route ---------------------------------------------

_STATUS_PATH = "/api/channels/twilio/status"
_STATUS_ROUTE = "/status"


@pytest.fixture
def status_handler(stub_app) -> Callable[..., Awaitable[Response]]:
    routes = [route for route in stub_app.http.routes if route.path == _STATUS_ROUTE]
    assert len(routes) == 1
    route = routes[0]
    assert route.methods == ["POST"]
    assert route.authed is None
    return route.handler


def _status_pairs(status: str, sid: str = "SM777") -> list[tuple[str, str]]:
    return [("MessageSid", sid), ("MessageStatus", status)]


@pytest.mark.parametrize("status", ["failed", "undelivered"])
async def test_status_failed_records_failed(status_handler, stub_app, status: str):
    result = await status_handler(signed_request(_status_pairs(status), path=_STATUS_PATH))

    assert result.status_code == 204
    assert stub_app.conversations.status_calls == [
        {"channel": "twilio", "provider_message_id": "SM777", "status": DeliveryReceipt.FAILED}
    ]


async def test_status_delivered_records_delivered(status_handler, stub_app):
    result = await status_handler(signed_request(_status_pairs("delivered"), path=_STATUS_PATH))

    assert result.status_code == 204
    assert stub_app.conversations.status_calls == [
        {"channel": "twilio", "provider_message_id": "SM777", "status": DeliveryReceipt.DELIVERED}
    ]


@pytest.mark.parametrize("status", ["queued", "sent", "sending"])
async def test_status_intermediate_is_benign_noop(status_handler, stub_app, status: str):
    result = await status_handler(signed_request(_status_pairs(status), path=_STATUS_PATH))

    assert result.status_code == 204
    assert stub_app.conversations.status_calls == []  # nothing recorded for a non-terminal status


async def test_status_unknown_sid_is_benign_204(status_handler, stub_app, caplog: pytest.LogCaptureFixture):
    # record_delivery_status raises LookupError for a sid the bridge does not track;
    # the route acks rather than 5xx-ing the provider for a message we do not own.
    stub_app.conversations.status_error = LookupError("outbound id SM777 maps to no answer record")

    with caplog.at_level("INFO"):
        result = await status_handler(signed_request(_status_pairs("failed"), path=_STATUS_PATH))

    assert result.status_code == 204
    assert any("untracked message SM777" in record.message for record in caplog.records)


async def test_status_flow_send_hit_posts_receipt_no_untracked_log(
    status_handler, stub_app, caplog: pytest.LogCaptureFixture
):
    # The bridge does not own the sid (LookupError), but it is a flow send: the flow-send
    # receipt seam resolves it and posts the receipt onto the trace, so NO untracked log.
    stub_app.conversations.status_error = LookupError("outbound id SM777 maps to no answer record")
    stub_app.channels.flow_receipt_result = True

    with caplog.at_level("INFO"):
        result = await status_handler(signed_request(_status_pairs("delivered"), path=_STATUS_PATH))

    assert result.status_code == 204
    (call,) = stub_app.channels.flow_receipt_calls
    assert call["channel"] == "twilio"
    assert call["provider_message_id"] == "SM777"
    assert call["status"] is DeliveryReceipt.DELIVERED
    assert not any("untracked message SM777" in record.message for record in caplog.records)


async def test_status_bad_signature_is_401(status_handler, stub_app):
    result = await status_handler(signed_request(_status_pairs("failed"), path=_STATUS_PATH, token="other-token"))

    assert result.status_code == 401
    assert stub_app.conversations.status_calls == []


async def test_status_missing_sid_is_400(status_handler, stub_app):
    result = await status_handler(signed_request([("MessageStatus", "delivered")], path=_STATUS_PATH))

    assert result.status_code == 400
    assert stub_app.conversations.status_calls == []


async def test_status_missing_status_is_400(status_handler, stub_app):
    result = await status_handler(signed_request([("MessageSid", "SM777")], path=_STATUS_PATH))

    assert result.status_code == 400
    assert stub_app.conversations.status_calls == []
