"""Send order inside one ``notify``/``deliver``: every provider message but the first waits for the
medium to report the previous one delivered (``tai42_app.conversations.await_delivery``)."""

from __future__ import annotations

import pytest
from tai42_contract.channels import ChannelDeliveryError, ChannelNotification, ReplyOption
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind

from tai42_channel_whatsapp.channel import WhatsAppChannel

from .conftest import ALLOWED_A, FakeHttpx, FakeRedis, _accepted, make_delivery

pytestmark = pytest.mark.usefixtures("whatsapp_env")


def _sends_and_holds(fake_httpx: FakeHttpx) -> list[tuple[str, str]]:
    """The message POSTs and delivery holds in the order they happened (a POST as ``("send", "")``)."""
    order: list[tuple[str, str]] = []
    for kind, value in fake_httpx.events:
        if kind == "http_post":
            order.append(("send", ""))
        elif kind == "await_delivery":
            order.append(("await", value))
    return order


async def test_notify_body_location_and_media_each_wait_for_the_previous_delivery(
    stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx
):
    for wamid in ("wamid.BODY", "wamid.LOC", "wamid.IMG1", "wamid.IMG2"):
        fake_httpx.responses.append(_accepted(wamid))

    outcome = await WhatsAppChannel().notify(
        ChannelNotification(
            message="Here it is",
            recipient=ALLOWED_A,
            location=LocationElement(latitude=1.0, longitude=2.0),
            media=[
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/b.jpg"),
            ],
        )
    )

    assert outcome.message_ids == ["wamid.BODY", "wamid.LOC", "wamid.IMG1", "wamid.IMG2"]
    assert _sends_and_holds(fake_httpx) == [
        ("send", ""),
        ("await", "wamid.BODY"),
        ("send", ""),
        ("await", "wamid.LOC"),
        ("send", ""),
        ("await", "wamid.IMG1"),
        ("send", ""),
    ]
    assert {channel for channel, _ in stub_app.conversations.await_delivery_calls} == {"whatsapp"}


async def test_notify_single_message_waits_for_nothing(stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.ONE"))

    await WhatsAppChannel().notify(ChannelNotification(message="Hello", recipient=ALLOWED_A))

    assert stub_app.conversations.await_delivery_calls == []


async def test_notify_leading_header_waits_before_the_interactive(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.AUDIO"))
    fake_httpx.responses.append(_accepted("wamid.INT"))

    await WhatsAppChannel().notify(
        ChannelNotification(
            message="Listen then choose",
            recipient=ALLOWED_A,
            options=[ReplyOption(text="OK")],
            header=MediaItem(kind=MediaKind.AUDIO, url="https://cdn.example/a.mp3"),
        )
    )

    assert _sends_and_holds(fake_httpx) == [("send", ""), ("await", "wamid.AUDIO"), ("send", "")]


async def test_deliver_media_prelude_waits_before_each_next_message(fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    for wamid in ("wamid.LINKS", "wamid.IMG", "wamid.Q"):
        fake_httpx.responses.append(_accepted(wamid))

    await WhatsAppChannel().deliver(
        make_delivery(
            answer_format="text",
            media=[
                MediaItem(kind=MediaKind.LINK, url="https://docs.example/p/1", caption="Details page"),
                MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg"),
            ],
        )
    )

    assert _sends_and_holds(fake_httpx) == [
        ("send", ""),
        ("await", "wamid.LINKS"),
        ("send", ""),
        ("await", "wamid.IMG"),
        ("send", ""),
    ]


async def test_notify_failed_receipt_stops_the_sequence_loudly(stub_app, fake_redis: FakeRedis, fake_httpx: FakeHttpx):
    fake_httpx.responses.append(_accepted("wamid.BODY"))
    stub_app.conversations.await_delivery_error = ChannelDeliveryError("wamid.BODY failed: 131026", retryable=False)

    with pytest.raises(ChannelDeliveryError, match="131026"):
        await WhatsAppChannel().notify(
            ChannelNotification(
                message="Here it is",
                recipient=ALLOWED_A,
                media=[MediaItem(kind=MediaKind.IMAGE, url="https://cdn.example/a.jpg")],
            )
        )

    assert len(fake_httpx.calls) == 1  # the image is never sent
