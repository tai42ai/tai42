"""The WhatsApp 24-hour reply window over Twilio: every inbound on a ``whatsapp:`` pair advances it; SMS has none."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from starlette.responses import Response
from tai42_contract.channels import AnswerForwardError, ChannelDeliveryError, InboundAnswerOutcome, ReplyWindow

import tai42_channel_twilio.inbound  # noqa: F401  (route registration side-effect)
from tai42_channel_twilio.channel import TwilioChannel

from .conftest import FakeHttpx, FakeRedis, signed_request

pytestmark = pytest.mark.usefixtures("twilio_env")

DAY_MS = 86_400_000
_ROUTE = "/inbound"
_WA_TWILIO = "whatsapp:+15550000001"
_WA_HUMAN = "whatsapp:+15550000009"
_WA_KEY = f"channel:twilio:reply-window:{_WA_TWILIO}:{_WA_HUMAN}"
_SMS_TWILIO = "+15550000001"
_SMS_HUMAN = "+15550000002"


def _ms(value: datetime) -> int:
    return (value - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


@pytest.fixture
def handler(stub_app) -> Callable[..., Awaitable[Response]]:
    (route,) = [route for route in stub_app.http.routes if route.path == _ROUTE]
    return route.handler


def _pairs(*, sid: str = "SM900", to: str = _WA_TWILIO, sender: str = _WA_HUMAN) -> list[tuple[str, str]]:
    return [("MessageSid", sid), ("To", to), ("From", sender), ("Body", "hello")]


def _window_keys(fake_redis: FakeRedis) -> list[str]:
    return [key for key in fake_redis.store if key.startswith("channel:twilio:reply-window:")]


# --- What an inbound writes ---------------------------------------------------


async def test_a_whatsapp_inbound_opens_the_window_for_a_day_from_receipt(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    before = _ms(datetime.now(UTC))
    result = await handler(signed_request(_pairs()))
    after = _ms(datetime.now(UTC))

    assert result.status_code == 204
    stored = int(fake_redis.store[_WA_KEY])
    assert before + DAY_MS <= stored <= after + DAY_MS
    assert fake_redis.pxat[_WA_KEY] == stored


async def test_a_correlated_whatsapp_answer_opens_the_window_too(
    handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert len(channels.inbound_calls) == 1
    assert _WA_KEY in fake_redis.store


async def test_an_sms_inbound_writes_no_window(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    await handler(signed_request(_pairs(to=_SMS_TWILIO, sender=_SMS_HUMAN)))

    assert _window_keys(fake_redis) == []


async def test_a_duplicate_message_sid_writes_no_window(
    handler, stub_app, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    fake_redis.store["channel:twilio:seen:SM900"] = "1"

    result = await handler(signed_request(_pairs()))

    assert result.status_code == 204
    assert _window_keys(fake_redis) == []


async def test_a_retried_message_sid_keeps_the_window_its_first_receipt_opened(
    handler, channels, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    channels.inbound_error = AnswerForwardError("HTTP 500")
    with pytest.raises(AnswerForwardError):
        await handler(signed_request(_pairs(sid="SM901")))
    first = int(fake_redis.store[_WA_KEY])
    await asyncio.sleep(0.01)

    channels.inbound_error = None
    result = await handler(signed_request(_pairs(sid="SM901")))

    assert result.status_code == 204
    assert int(fake_redis.store[_WA_KEY]) == first
    assert fake_redis.pxat[_WA_KEY] == first


async def test_the_first_receipt_of_a_message_sid_is_kept_for_the_window_length(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    before = _ms(datetime.now(UTC))
    await handler(signed_request(_pairs(sid="SM902")))
    after = _ms(datetime.now(UTC))

    received_key = "channel:twilio:first-received:SM902"
    assert before <= int(fake_redis.store[received_key]) <= after
    assert fake_redis.ttls[received_key] >= DAY_MS // 1000
    assert int(fake_redis.store[_WA_KEY]) == int(fake_redis.store[received_key]) + DAY_MS


async def test_a_corrupt_first_receipt_record_raises(handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_redis.store["channel:twilio:first-received:SM903"] = "garbage"

    with pytest.raises(ValueError, match="not an integer"):
        await handler(signed_request(_pairs(sid="SM903")))
    assert _window_keys(fake_redis) == []


async def test_a_later_inbound_moves_the_window_forward(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    fake_redis.store[_WA_KEY] = str(_ms(datetime.now(UTC)) + 1000)

    await handler(signed_request(_pairs()))

    assert int(fake_redis.store[_WA_KEY]) > _ms(datetime.now(UTC)) + DAY_MS - 5000


# --- What the channel reports -------------------------------------------------


async def test_an_sms_recipient_has_no_window(stub_app, fake_redis: FakeRedis):
    assert await TwilioChannel().reply_window(recipient=_SMS_HUMAN) == ReplyWindow(state="none", closes_at=None)
    assert await TwilioChannel().reply_window(recipient=_SMS_HUMAN, sender_identity=_SMS_TWILIO) == ReplyWindow(
        state="none", closes_at=None
    )


async def test_a_fresh_whatsapp_pair_reads_open_until_its_close_time(
    handler, stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    await handler(signed_request(_pairs()))
    closes_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(fake_redis.store[_WA_KEY]))

    window = await TwilioChannel().reply_window(recipient=_WA_HUMAN, sender_identity=_WA_TWILIO)

    assert window == ReplyWindow(state="open", closes_at=closes_at)


async def test_the_reported_close_time_is_whole_seconds_rounded_down(stub_app, fake_redis: FakeRedis):
    whole_second = (datetime.now(UTC) + timedelta(hours=1)).replace(microsecond=0)
    fake_redis.store[_WA_KEY] = str(_ms(whole_second) + 456)

    window = await TwilioChannel().reply_window(recipient=_WA_HUMAN, sender_identity=_WA_TWILIO)

    assert window == ReplyWindow(state="open", closes_at=whole_second)
    dumped = window.model_dump(mode="json")["closes_at"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", dumped)
    assert dumped == whole_second.strftime("%Y-%m-%dT%H:%M:%SZ")


async def test_a_whatsapp_pair_without_a_message_reads_closed(stub_app, fake_redis: FakeRedis):
    window = await TwilioChannel().reply_window(recipient=_WA_HUMAN, sender_identity=_WA_TWILIO)

    assert window == ReplyWindow(state="closed", closes_at=None)


async def test_no_sender_identity_reads_the_configured_from_number(
    stub_app, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("CHANNEL_TWILIO_FROM", _WA_TWILIO)
    reset_all_settings()
    fake_redis.store[_WA_KEY] = str(_ms(datetime.now(UTC)) + 60_000)

    window = await TwilioChannel().reply_window(recipient=_WA_HUMAN)

    assert window.state == "open"


async def test_no_sender_identity_and_no_from_number_is_a_delivery_error(
    stub_app, fake_redis: FakeRedis, monkeypatch: pytest.MonkeyPatch
):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TWILIO_FROM")
    reset_all_settings()

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_TWILIO_FROM"):
        await TwilioChannel().reply_window(recipient=_WA_HUMAN)


async def test_a_corrupt_stored_value_raises(stub_app, fake_redis: FakeRedis):
    fake_redis.store[_WA_KEY] = "garbage"

    with pytest.raises(ValueError, match="not an integer"):
        await TwilioChannel().reply_window(recipient=_WA_HUMAN, sender_identity=_WA_TWILIO)
