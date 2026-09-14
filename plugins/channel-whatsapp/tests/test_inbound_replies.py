"""Reply-capable content handlers — text replies, interactive button/list taps,
and their dedupe / correlation / bridge routing."""

from __future__ import annotations

import pytest
from tai42_contract.channels import InboundAnswerOutcome
from tai42_contract.conversations import BlankInboundTextError

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)

from .conftest import (
    _PENDING_KEY,
    _SEEN_KEY,
    _WAMID,
    PHONE_NUMBER_ID,
    WA_ID,
    FakeHttpx,
    FakeRedis,
    _params_envelope,
    _pending_intact,
    _seed_pending,
    _seed_pending_select,
    interactive_payload,
    message_payload,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")


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
