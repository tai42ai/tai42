"""Outbound HTTP for the Telegram channel.

One pooled ``httpx.AsyncClient`` (the kit's ``HttpxClient`` via
``tai42_app.clients.client_ctx``) serves every outbound call: ``sendMessage`` /
``setWebhook`` and the loopback answer forward. Pooled per event loop + timeout;
``trust_env=False`` ignores ambient proxy env vars.
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.settings import require_secret

from tai42_channel_telegram.settings import telegram_settings


def telegram_http() -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """A pooled outbound client budgeted by ``CHANNEL_TELEGRAM_HTTP_TIMEOUT_SECONDS``."""
    return tai42_app.clients.client_ctx(HttpxClient, timeout=telegram_settings().http_timeout_seconds)


async def send_chat_action(chat_id: int, action: str) -> None:
    """POST one Bot API ``sendChatAction`` so ``chat_id`` shows a status indicator.

    Fire-and-forget from the caller's view: returns on the Bot API's ``ok: true``
    and raises :class:`~tai42_contract.channels.ChannelDeliveryError` on an unset
    token, a transport error, a non-200 status, a non-JSON body, or ``ok: false``.
    The request URL embeds the bot token and never appears in error text.
    """
    settings = telegram_settings()
    try:
        token = require_secret(settings.bot_token, "the telegram channel", "CHANNEL_TELEGRAM_BOT_TOKEN")
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc
    try:
        async with telegram_http() as client:
            response = await client.post(
                f"{settings.api_base_url}/bot{token}/sendChatAction",
                json={"chat_id": chat_id, "action": action},
            )
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"telegram sendChatAction failed: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(
            f"telegram sendChatAction returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError("telegram sendChatAction returned a non-JSON body") from exc
    if not data.get("ok"):
        raise ChannelDeliveryError(
            f"telegram sendChatAction rejected: "
            f"error_code={data.get('error_code')} description={data.get('description')!r}"
        )
