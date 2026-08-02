"""The Telegram :class:`~tai42_contract.channels.Channel`.

``deliver`` validates the bot token (``CHANNEL_TELEGRAM_BOT_TOKEN``) first, then
resolves the recipient chat: a caller-supplied ``delivery.recipient`` must be on
``CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS`` (fail closed) else the operator-set
``CHANNEL_TELEGRAM_DEFAULT_RECIPIENT``. EVERY failure on the deliver/notify path
raises :class:`~tai42_contract.channels.ChannelDeliveryError`, operator
misconfiguration included. The question goes as ONE ``sendMessage`` (the Bot API
has no idempotency key, so a failed send raises rather than risk a duplicate).

Tier-2 (``text``/``select``) carries ``reply_markup: {force_reply: true}`` so the
reply arrives with ``reply_to_message``; the ``message_id -> callback_url``
mapping is stored before ``deliver`` returns so the inbound door can route the
answer. Tier-1 (``confirm``/``external``) carries a tappable URL button to the
callback door instead — no correlation, no inbound involvement.

``notify`` is fire-and-forget: ONE plain ``sendMessage`` (``chat_id`` + ``text``
only), returning the sent ``message_id``. The recipient allowlist governs
default/ask_user sends; a bridge reply carries ``sender_identity`` (this bot's
numeric id) and goes to the initiating chat verbatim — a mismatched
``sender_identity`` is refused.

Error text never includes the request URL — the bot token is embedded in it.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import SecretStr
from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification
from tai42_kit.settings import require, require_secret

from tai42_channel_telegram.client import telegram_http
from tai42_channel_telegram.correlation import store_correlation
from tai42_channel_telegram.settings import bot_numeric_id, telegram_settings

# Tier-1 (confirm/external) is answered at the callback door via a tappable URL
# button; text/select are Tier-2 (ForceReply + correlation, answered by typing).
_TIER1_FORMATS = frozenset({"confirm", "external"})


def _question_text(delivery: ChannelDelivery) -> str:
    """Render the question for a plain-text chat, format-aware: a select question
    lists its options as guided text; the deadline is surfaced."""
    lines = [delivery.question]
    if delivery.answer_format == "select" and delivery.options:
        lines.append("")
        lines.extend(f"- {option}" for option in delivery.options)
        lines.append("")
        lines.append("Reply with one of the options above.")
    lines.append(f"(Answer before {delivery.timeout_at.strftime('%Y-%m-%d %H:%M %Z')}.)")
    return "\n".join(lines)


def _require_delivery[T](value: T | None, env_name: str) -> T:
    """The configured value, or raise :class:`ChannelDeliveryError` naming the
    missing env var (retyping :func:`require` for the deliver/notify path)."""
    try:
        return require(value, "the telegram channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _require_delivery_secret(value: SecretStr | None, env_name: str) -> str:
    """The secret's plaintext, or raise :class:`ChannelDeliveryError` on
    unset/EMPTY (fail CLOSED; retyping :func:`require_secret`, message names only
    the env var)."""
    try:
        return require_secret(value, "the telegram channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _bot_identity(token: str) -> str:
    """This bot's numeric id, or raise :class:`ChannelDeliveryError` on a
    malformed token (retyping :func:`bot_numeric_id` for the send path)."""
    try:
        return bot_numeric_id(token)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _resolve_target(recipient: str | None) -> str:
    """Resolve the target chat id, fail closed against the operator allowlist.

    A caller-supplied ``recipient`` must be on
    ``CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS`` or the send is refused; ``None`` uses
    ``CHANNEL_TELEGRAM_DEFAULT_RECIPIENT`` (must be set). The allowlist gates only
    caller-supplied values.
    """
    settings = telegram_settings()
    if recipient is None:
        return _require_delivery(settings.default_recipient, "CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")
    if recipient not in set(settings.allowed_recipients):
        raise ChannelDeliveryError(
            f"recipient {recipient!r} is not on CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS; refusing to send"
        )
    return recipient


async def _send_message(token: str, payload: dict[str, Any], context: str) -> dict[str, Any]:
    """POST ``payload`` as one Bot API ``sendMessage`` and validate the response.

    Returns the decoded ``ok: true`` body. A transport error, non-200 status,
    non-JSON body, or ``ok: false`` each raises
    :class:`~tai42_contract.channels.ChannelDeliveryError` naming ``context``.
    The request URL embeds the bot token and never appears in error text.
    """
    try:
        async with telegram_http() as client:
            response = await client.post(f"{telegram_settings().api_base_url}/bot{token}/sendMessage", json=payload)
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"telegram sendMessage failed for {context}: {type(exc).__name__}: {exc}") from exc

    if response.status_code != 200:
        raise ChannelDeliveryError(
            f"telegram sendMessage returned HTTP {response.status_code} for {context}: {response.text[:200]}"
        )
    try:
        data = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError(f"telegram sendMessage returned a non-JSON body for {context}") from exc
    if not data.get("ok"):
        raise ChannelDeliveryError(
            f"telegram sendMessage rejected {context}: "
            f"error_code={data.get('error_code')} description={data.get('description')!r}"
        )
    return data


class TelegramChannel:
    """Satisfies the :class:`~tai42_contract.channels.Channel` protocol.

    Stateless — settings are read at each send so a live-reload picks up rotated
    credentials with no stale per-instance snapshot.
    """

    async def deliver(self, delivery: ChannelDelivery) -> None:
        token = _require_delivery_secret(telegram_settings().bot_token, "CHANNEL_TELEGRAM_BOT_TOKEN")
        target = _resolve_target(delivery.recipient)

        if math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds()) <= 0:
            raise ChannelDeliveryError(
                f"interaction {delivery.interaction_id} already timed out "
                f"(timeout_at={delivery.timeout_at.isoformat()}); nothing was sent"
            )

        payload: dict[str, Any] = {"chat_id": target, "text": _question_text(delivery)}
        if delivery.answer_format in _TIER1_FORMATS:
            payload["reply_markup"] = {"inline_keyboard": [[{"text": "Answer", "url": delivery.callback_url}]]}
        else:
            payload["reply_markup"] = {"force_reply": True, "input_field_placeholder": "Reply to answer"}

        data = await _send_message(token, payload, f"interaction {delivery.interaction_id}")

        if delivery.answer_format in _TIER1_FORMATS:
            return

        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int):
            raise ChannelDeliveryError(
                f"telegram sendMessage ok response for interaction {delivery.interaction_id} "
                f"carried no result.message_id"
            )

        # Budget measured AFTER the send so the key expires at the deadline, not
        # deadline + send duration. A budget spent mid-send makes store_correlation
        # reject the non-positive TTL.
        ttl_seconds = math.ceil((delivery.timeout_at - datetime.now(UTC)).total_seconds())
        try:
            await store_correlation(message_id, delivery.callback_url, ttl_seconds)
        except Exception as exc:
            raise ChannelDeliveryError(
                f"question {delivery.interaction_id} was sent (message_id={message_id}) but its "
                f"correlation could not be stored; the reply cannot be routed"
            ) from exc

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Send ``notification.message`` as ONE plain ``sendMessage`` (fire-and-forget).

        With no ``sender_identity``: the allowlist gate of ``deliver``. With
        ``sender_identity`` set it must equal this bot's numeric id (else a typed
        refusal — never a send from the wrong face) and the message goes to
        ``recipient`` verbatim, allowlist bypassed. Returns the sent
        ``message_id`` as ``[str]``; any failure raises
        :class:`~tai42_contract.channels.ChannelDeliveryError`.
        """
        token = _require_delivery_secret(telegram_settings().bot_token, "CHANNEL_TELEGRAM_BOT_TOKEN")
        if notification.sender_identity is not None:
            our_identity = _bot_identity(token)
            if notification.sender_identity != our_identity:
                raise ChannelDeliveryError(
                    f"sender_identity {notification.sender_identity!r} is not this bot's identity; refusing to send"
                )
            target = notification.recipient if notification.recipient is not None else _resolve_target(None)
        else:
            target = _resolve_target(notification.recipient)

        data = await _send_message(token, {"chat_id": target, "text": notification.message}, "notification")
        result = data.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if not isinstance(message_id, int):
            raise ChannelDeliveryError("telegram sendMessage ok response for notification carried no result.message_id")
        return [str(message_id)]
