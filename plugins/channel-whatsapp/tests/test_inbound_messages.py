"""The per-message-type router — the known-contact marker, the read+typing signal,
and the error-notice / unhandled-type dispatch fallbacks."""

from __future__ import annotations

import pytest
from tai42_contract.channels import InboundAnswerOutcome
from tai42_contract.conversations import InboundRejectionReason

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)

from .conftest import (
    _CONTACT_KEY,
    _SEEN_KEY,
    _WAMID,
    PHONE_NUMBER_ID,
    WA_ID,
    FakeHttpx,
    FakeRedis,
    _params_envelope,
    _seed_pending,
    interactive_payload,
    message_payload,
    response,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")


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
    # A participant who sent an unmodelled type still opened Meta's window: the marker is
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


# --- Error-notice / unhandled-type dispatch fallbacks -------------------------


async def test_unsupported_type_error_notice_replies_and_warns(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A Meta inbound error notice whose code is 131051 ("Unsupported message type") names
    # participant content the channel cannot receive: still logged at WARNING and never bridged,
    # AND replied to once via the generic rejection facet, deduped per wamid.
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
    (rejected,) = stub_app.conversations.rejected_calls
    assert rejected["channel_id"] == "whatsapp"
    assert rejected["recipient"] == WA_ID
    assert rejected["sender_identity"] == PHONE_NUMBER_ID
    assert rejected["kind"] == "Unsupported message type"
    assert rejected["reason"] == InboundRejectionReason.UNSUPPORTED_TYPE
    assert any("error notice" in record.message and "131051" in record.getMessage() for record in caplog.records)
    assert _SEEN_KEY in fake_redis.store


async def test_other_code_error_notice_stays_warning_only(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # An error notice with any other code is an operator-facing vendor signal, not participant
    # content the channel failed to bridge: WARNING only, no reply, no event.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "unsupported",
        "errors": [{"code": 131047, "title": "Re-engagement message"}],
    }
    with caplog.at_level("WARNING"):
        result = await handler(signed_request(_params_envelope(message)))

    assert result.status_code == 200
    assert stub_app.conversations.rejected_calls == []
    assert stub_app.conversations.accept_calls == []
    assert any("error notice" in record.message and "131047" in record.getMessage() for record in caplog.records)


async def test_unknown_content_type_replies_and_infos(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # An unhandled participant CONTENT type (not a non-content notification) is logged at INFO
    # AND replied to once via the generic rejection facet with the type as the kind.
    with caplog.at_level("INFO"):
        result = await handler(signed_request(message_payload(msg_type="hologram")))

    assert result.status_code == 200
    (rejected,) = stub_app.conversations.rejected_calls
    assert rejected["kind"] == "hologram"
    assert rejected["recipient"] == WA_ID
    assert rejected["sender_identity"] == PHONE_NUMBER_ID
    assert rejected["reason"] == InboundRejectionReason.UNSUPPORTED_TYPE
    assert any(
        record.levelname == "INFO" and "hologram" in record.getMessage() and "unhandled" in record.getMessage()
        for record in caplog.records
    )
    assert _SEEN_KEY in fake_redis.store


@pytest.mark.parametrize("msg_type", ["system", "request_welcome"])
async def test_non_content_notice_type_stays_info_only(
    msg_type: str, handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture
):
    # A vendor non-content notification (system / welcome-request) is not participant content:
    # logged at INFO, never replied to, never marked seen.
    with caplog.at_level("INFO"):
        result = await handler(signed_request(message_payload(msg_type=msg_type)))

    assert result.status_code == 200
    assert stub_app.conversations.rejected_calls == []
    assert stub_app.conversations.accept_calls == []
    assert _SEEN_KEY not in fake_redis.store
    assert any(
        record.levelname == "INFO" and msg_type in record.getMessage() and "unhandled" in record.getMessage()
        for record in caplog.records
    )


async def test_duplicate_unsupported_delivery_sends_no_second_reply(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    # A Meta redelivery of the same unsupported-type notice (same wamid) is short-circuited by
    # already_seen: exactly one reply, never a second.
    message = {
        "id": _WAMID,
        "from": WA_ID,
        "type": "unsupported",
        "errors": [{"code": 131051, "title": "Unsupported message type"}],
    }
    await handler(signed_request(_params_envelope(message)))
    await handler(signed_request(_params_envelope(message)))

    assert len(stub_app.conversations.rejected_calls) == 1


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
