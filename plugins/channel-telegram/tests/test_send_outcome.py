"""What a Telegram send reports about itself: the pre-send address and the completed send's outcome.

``resolve_address`` answers the identity and the target a send would use, the target as the
caller names it (an ``@username`` included). A completed send's :class:`SendOutcome` carries the
numeric chat id Telegram resolved the send to, the same ``client_address`` the inbound door
passes for a message from that chat, and every ``message_id`` sent, in order.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from tai42_contract.channels import (
    ChannelAddress,
    ChannelAddressError,
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelNotification,
)
from tai42_contract.interactions.models import LocationElement, MediaItem, MediaKind
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.channel import TelegramChannel
from tai42_channel_telegram.inbound import inbound

from .conftest import make_inbound_request

_BOT = "123456"
_HANDLE = "@name"
_CHAT = 12345
_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "s3cret_token"}


@pytest.fixture
def handle_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """``@name`` on the recipient allowlist beside the numeric ids."""
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", f"888,999,{_HANDLE}")
    reset_all_settings()


def _numbered(chat_id: int = _CHAT):
    """A responder answering each send with the next ``message_id`` from 50, in chat ``chat_id``."""
    counter = iter(range(50, 100))

    def responder(request: httpx.Request) -> httpx.Response:
        result = {"message_id": next(counter), "chat": {"id": chat_id}}
        return httpx.Response(200, json={"ok": True, "result": result})

    return responder


def _delivery(
    answer_format: str = "text",
    *,
    recipient: str | None = None,
    media: list[MediaItem] | None = None,
    schema: dict[str, Any] | None = None,
) -> ChannelDelivery:
    return ChannelDelivery(
        interaction_id="int-1",
        question="Which one?",
        answer_format=answer_format,
        media=media,
        schema=schema,
        callback_url="https://example.test/api/interactions/callback/tkt",
        timeout_at=datetime.now(UTC) + timedelta(seconds=600),
        recipient=recipient,
        sensitive=False,
    )


# -- resolve_address ---------------------------------------------------------------------------


def test_resolve_address_with_no_recipient_is_the_bot_and_the_default_chat():
    assert TelegramChannel().resolve_address(None, None) == ChannelAddress(our_identity=_BOT, address="777")


def test_resolve_address_keeps_an_allowlisted_recipient():
    assert TelegramChannel().resolve_address("888", None) == ChannelAddress(our_identity=_BOT, address="888")


def test_resolve_address_returns_an_allowlisted_handle_as_given(handle_allowed):
    assert TelegramChannel().resolve_address(_HANDLE, None).address == _HANDLE


def test_resolve_address_refuses_a_recipient_off_the_allowlist():
    with pytest.raises(ChannelAddressError, match="not on CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS"):
        TelegramChannel().resolve_address("666", None)


def test_resolve_address_with_no_recipient_and_no_default_is_an_address_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")
    reset_all_settings()
    with pytest.raises(ChannelAddressError, match="CHANNEL_TELEGRAM_DEFAULT_RECIPIENT"):
        TelegramChannel().resolve_address(None, None)


def test_resolve_address_with_this_bots_identity_takes_the_recipient_verbatim():
    assert TelegramChannel().resolve_address("555", _BOT) == ChannelAddress(our_identity=_BOT, address="555")


def test_resolve_address_refuses_another_bots_identity():
    with pytest.raises(ChannelDeliveryError, match="is not this bot's identity") as refused:
        TelegramChannel().resolve_address("555", "999999")
    assert not isinstance(refused.value, ChannelAddressError)


def test_resolve_address_with_a_malformed_token_is_a_delivery_error(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_BOT_TOKEN", "no-colon-token")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError) as refused:
        TelegramChannel().resolve_address(None, None)
    assert not isinstance(refused.value, ChannelAddressError)


# -- the outcome of a completed send -----------------------------------------------------------


async def test_a_notify_to_a_handle_reports_the_numeric_chat_the_inbound_door_names(
    handle_allowed, http_recorder, fake_redis, conversations
):
    http_recorder.responder = _numbered()

    outcome = await TelegramChannel().notify(ChannelNotification(message="ping", recipient=_HANDLE))

    assert json.loads(http_recorder.requests[0].content)["chat_id"] == _HANDLE
    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (_BOT, "12345", ["50"])
    update = {"update_id": 3, "message": {"message_id": 7, "chat": {"id": _CHAT}, "text": "hi"}}
    await inbound(make_inbound_request(update, headers=_HEADERS))
    (call,) = conversations.accept_calls
    assert (call.our_identity, call.client_address) == (outcome.our_identity, outcome.address)


async def test_a_multi_part_notify_reports_every_message_in_send_order(http_recorder, fake_redis):
    http_recorder.responder = _numbered()
    media = [MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png")]
    location = LocationElement(latitude=1.5, longitude=2.5)

    outcome = await TelegramChannel().notify(ChannelNotification(message="Here:", media=media, location=location))

    assert outcome.address == "12345"
    assert outcome.message_ids == ["50", "51", "52"]


async def test_a_bridge_reply_notify_reports_this_bot_and_the_chat(http_recorder, fake_redis):
    http_recorder.responder = _numbered()

    outcome = await TelegramChannel().notify(ChannelNotification(message="hi", recipient="555", sender_identity=_BOT))

    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (_BOT, "12345", ["50"])


async def test_a_send_whose_response_names_no_chat_raises(http_recorder, fake_redis):
    http_recorder.responder = lambda request: httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})

    with pytest.raises(ChannelDeliveryError, match=r"carried no result\.chat\.id"):
        await TelegramChannel().notify(ChannelNotification(message="ping"))


async def test_a_deliver_to_a_handle_reports_the_numeric_chat(handle_allowed, http_recorder, fake_redis):
    http_recorder.responder = _numbered()

    outcome = await TelegramChannel().deliver(_delivery(recipient=_HANDLE))

    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (_BOT, "12345", ["50"])


async def test_a_deliver_with_media_reports_the_media_then_the_question(http_recorder, fake_redis):
    http_recorder.responder = _numbered()
    media = [MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png")]

    outcome = await TelegramChannel().deliver(_delivery(media=media))

    assert outcome.address == "12345"
    assert outcome.message_ids == ["50", "51"]


@pytest.mark.parametrize("answer_format", ["confirm", "external", "form"])
async def test_a_link_answered_deliver_reports_its_message(http_recorder, fake_redis, answer_format: str):
    http_recorder.responder = _numbered()
    schema = {"type": "object", "properties": {"name": {"type": "string"}}} if answer_format == "form" else None
    delivery = _delivery(answer_format, schema=schema)

    outcome = await TelegramChannel().deliver(delivery)

    assert (outcome.our_identity, outcome.address, outcome.message_ids) == (_BOT, "12345", ["50"])
