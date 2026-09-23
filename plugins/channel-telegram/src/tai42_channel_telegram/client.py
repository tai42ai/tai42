"""Outbound HTTP for the Telegram channel.

One pooled ``httpx.AsyncClient`` (the kit's ``HttpxClient`` via
``tai42_app.clients.client_ctx``) serves every outbound call: ``sendMessage`` /
``setWebhook`` and the loopback answer forward. Pooled per event loop + timeout;
``trust_env=False`` ignores ambient proxy env vars.
"""

from __future__ import annotations

import json
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.settings import require_secret

from tai42_channel_telegram.settings import telegram_settings


def telegram_http() -> AbstractAsyncContextManager[httpx.AsyncClient]:
    """A pooled outbound client budgeted by ``CHANNEL_TELEGRAM_HTTP_TIMEOUT_SECONDS``."""
    return tai42_app.clients.client_ctx(HttpxClient, timeout=telegram_settings().http_timeout_seconds)


# The Bot API's error object, in the vendor's documented field order.
_ERROR_FIELDS = ("error_code", "description", "parameters")


def _error_detail(response: httpx.Response) -> str:
    """The Bot API's full documented error object from the body, whatever the status.

    Telegram answers a JSON error object on an HTTP-error status too, so the body is
    parsed regardless of status. Each present field renders as a ``name=<render>``
    token in the Bot API's fixed order — ``repr(value)`` for a scalar, compact sorted
    JSON for ``parameters`` — joined by one space and bounded to 500 chars.
    ``parameters`` carries ``retry_after`` / ``migrate_to_chat_id``. A body that is
    not JSON, or not a dict, falls back to the raw response text (also bounded).
    """
    try:
        payload = response.json()
    except ValueError:
        return response.text[:500]
    if not isinstance(payload, dict):
        return response.text[:500]
    tokens: list[str] = []
    for name in _ERROR_FIELDS:
        if name not in payload:
            continue
        value = payload[name]
        if isinstance(value, (list, dict)):
            render = json.dumps(value, sort_keys=True, separators=(",", ":"))
        else:
            render = repr(value)
        tokens.append(f"{name}={render}")
    return " ".join(tokens)[:500]


async def call_method(token: str, method: str, payload: dict[str, Any], *, context: str) -> dict[str, Any]:
    """POST ``payload`` to one Bot API ``method`` and return its decoded ``ok: true`` body.

    The single transport for every Bot API call. It posts to
    ``{api_base_url}/bot{token}/{method}``, wraps an ``httpx.HTTPError`` as a
    :class:`~tai42_contract.channels.ChannelDeliveryError`, then applies one refusal
    rule: a non-200 status OR a JSON body with ``ok`` false raises, naming ``method``
    and ``context`` and carrying the vendor's full error detail. A 200 whose body is
    not JSON also raises. The request URL embeds the bot token and never appears in
    error text.
    """
    settings = telegram_settings()
    try:
        async with telegram_http() as client:
            response = await client.post(f"{settings.api_base_url}/bot{token}/{method}", json=payload)
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"telegram {method} failed for {context}: {type(exc).__name__}: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(f"telegram {method} rejected {context}: {_error_detail(response)}")
    try:
        data = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError(f"telegram {method} returned a non-JSON body for {context}") from exc
    if not data.get("ok"):
        raise ChannelDeliveryError(f"telegram {method} rejected {context}: {_error_detail(response)}")
    return data


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
    await call_method(token, "sendChatAction", {"chat_id": chat_id, "action": action}, context=f"chat {chat_id}")


async def answer_callback_query(callback_query_id: str) -> None:
    """POST one Bot API ``answerCallbackQuery`` so the tapped inline button stops showing its loading spinner.

    Fire-and-forget from the caller's view: returns on the Bot API's ``ok: true``
    and raises :class:`~tai42_contract.channels.ChannelDeliveryError` on an unset
    token, a transport error, a non-200 status, a non-JSON body, or ``ok: false``
    — the inbound door swallows that failure (an unanswered callback only leaves a
    spinner; it must never fail the webhook and trigger a redelivery). The request
    URL embeds the bot token and never appears in error text.
    """
    settings = telegram_settings()
    try:
        token = require_secret(settings.bot_token, "the telegram channel", "CHANNEL_TELEGRAM_BOT_TOKEN")
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc
    await call_method(
        token,
        "answerCallbackQuery",
        {"callback_query_id": callback_query_id},
        context=f"callback {callback_query_id}",
    )
