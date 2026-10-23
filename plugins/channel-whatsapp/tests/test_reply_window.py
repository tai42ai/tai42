"""The 24-hour reply window: every client message advances it from Meta's ``timestamp``; the channel reports it."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.channels import ChannelDeliveryError, ReplyWindow

import tai42_channel_whatsapp.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_whatsapp.channel import WhatsAppChannel

from .conftest import (
    _CONTACT_KEY,
    _REPLY_WINDOW_KEY,
    PHONE_NUMBER_ID,
    WA_ID,
    FakeHttpx,
    FakeRedis,
    interactive_payload,
    message_payload,
    signed_request,
)

pytestmark = pytest.mark.usefixtures("whatsapp_env")

DAY_MS = 86_400_000
_LOGGER = "tai42_channel_whatsapp.inbound.messages"


def _ms(value: datetime) -> int:
    return (value - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


# --- What an inbound writes ---------------------------------------------------


async def test_a_text_inbound_opens_the_window_for_a_day_from_its_timestamp(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    sent = int(time.time()) - 60
    await handler(signed_request(message_payload(text="hello", timestamp=str(sent))))

    assert fake_redis.store[_REPLY_WINDOW_KEY] == str(sent * 1000 + DAY_MS)
    assert fake_redis.pxat[_REPLY_WINDOW_KEY] == sent * 1000 + DAY_MS


@pytest.mark.parametrize("msg_type", ["image", "reaction", "unsupported"])
async def test_other_client_message_types_open_the_window_too(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, msg_type: str
):
    sent = int(time.time()) - 5
    await handler(signed_request(message_payload(msg_type=msg_type, timestamp=str(sent))))

    assert fake_redis.store[_REPLY_WINDOW_KEY] == str(sent * 1000 + DAY_MS)


@pytest.mark.parametrize("msg_type", ["system", "request_welcome"])
async def test_a_platform_notice_does_not_open_the_window(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, msg_type: str
):
    await handler(signed_request(message_payload(msg_type=msg_type)))

    assert _CONTACT_KEY in fake_redis.store  # the known-contact marker is a separate concept, unchanged
    assert _REPLY_WINDOW_KEY not in fake_redis.store


@pytest.mark.parametrize("timestamp", [None, "", "soon", "12.5", "-5", "9" * 30, {"seconds": 1}, True])
async def test_a_missing_or_unreadable_timestamp_warns_and_writes_nothing(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx, caplog: pytest.LogCaptureFixture, timestamp
):
    caplog.set_level(logging.WARNING, logger=_LOGGER)
    result = await handler(signed_request(message_payload(wamid="wamid.NOSTAMP", timestamp=timestamp)))

    assert result.status_code == 200
    assert _REPLY_WINDOW_KEY not in fake_redis.store
    warnings = [r for r in caplog.records if r.name == _LOGGER and r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "wamid.NOSTAMP" in warnings[0].getMessage()
    assert "timestamp" in warnings[0].getMessage()
    assert stub_app.conversations.accept_calls  # the message itself is still bridged


async def test_an_interactive_tap_without_a_timestamp_is_still_bridged(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await handler(signed_request(interactive_payload(reply_id="int-1:0", title="x")))

    assert _REPLY_WINDOW_KEY not in fake_redis.store


async def test_a_timestamp_in_the_future_is_clamped_to_now(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    before = _ms(datetime.now(UTC))
    await handler(signed_request(message_payload(timestamp=str(int(time.time()) + 3600))))
    after = _ms(datetime.now(UTC))

    stored = int(fake_redis.store[_REPLY_WINDOW_KEY])
    assert before + DAY_MS <= stored <= after + DAY_MS


async def test_an_older_redelivery_never_moves_the_window_back(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    newer = int(time.time()) - 10
    await handler(signed_request(message_payload(wamid="wamid.NEW", timestamp=str(newer))))
    await handler(signed_request(message_payload(wamid="wamid.OLD", timestamp=str(newer - 3600))))
    await handler(signed_request(message_payload(wamid="wamid.NEW", timestamp=str(newer))))

    assert fake_redis.store[_REPLY_WINDOW_KEY] == str(newer * 1000 + DAY_MS)


async def test_a_message_older_than_the_window_writes_nothing(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await handler(signed_request(message_payload(timestamp=str(int(time.time()) - 25 * 3600))))

    assert _REPLY_WINDOW_KEY not in fake_redis.store


# --- What the channel reports -------------------------------------------------


async def test_reply_window_is_open_after_an_inbound_then_closed_once_it_expires(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    sent = int(time.time()) - 60
    await handler(signed_request(message_payload(timestamp=str(sent))))

    window = await WhatsAppChannel().reply_window(recipient=WA_ID, sender_identity=PHONE_NUMBER_ID)
    assert window == ReplyWindow(state="open", closes_at=datetime.fromtimestamp(sent + 86_400, UTC))

    await fake_redis.delete(_REPLY_WINDOW_KEY)  # Redis drops the key at its PXAT
    window = await WhatsAppChannel().reply_window(recipient=WA_ID, sender_identity=PHONE_NUMBER_ID)
    assert window == ReplyWindow(state="closed", closes_at=None)


async def test_a_stored_close_time_already_past_reads_closed(stub_app, fake_redis: FakeRedis):
    fake_redis.store[_REPLY_WINDOW_KEY] = str(_ms(datetime.now(UTC)) - 1)

    window = await WhatsAppChannel().reply_window(recipient=WA_ID, sender_identity=PHONE_NUMBER_ID)
    assert window.state == "closed"


async def test_a_pair_that_never_wrote_reads_closed(stub_app, fake_redis: FakeRedis):
    assert await WhatsAppChannel().reply_window(recipient=WA_ID) == ReplyWindow(state="closed", closes_at=None)


async def test_no_sender_identity_reads_the_default_number(stub_app, fake_redis: FakeRedis):
    closes_at_ms = _ms(datetime.now(UTC).replace(microsecond=0)) + 3_600_000
    fake_redis.store[_REPLY_WINDOW_KEY] = str(closes_at_ms)
    fake_redis.store[f"channel:whatsapp:reply-window:OTHER-NUMBER:{WA_ID}"] = str(closes_at_ms + 1000)

    default = await WhatsAppChannel().reply_window(recipient=WA_ID)
    other = await WhatsAppChannel().reply_window(recipient=WA_ID, sender_identity="OTHER-NUMBER")

    assert default.closes_at == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=closes_at_ms)
    assert other.closes_at == datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=closes_at_ms + 1000)


async def test_no_sender_identity_and_no_default_number_is_a_delivery_error(
    stub_app, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_WHATSAPP_DEFAULT_PHONE_NUMBER_ID"):
        await WhatsAppChannel().reply_window(recipient=WA_ID)


async def test_a_corrupt_stored_value_raises(stub_app, fake_redis: FakeRedis):
    fake_redis.store[_REPLY_WINDOW_KEY] = "garbage"

    with pytest.raises(ValueError, match="not an integer"):
        await WhatsAppChannel().reply_window(recipient=WA_ID, sender_identity=PHONE_NUMBER_ID)
