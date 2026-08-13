"""The Slack ``Channel`` — outbound sends via ``chat.postMessage``: ``deliver``
(questions) and ``notify`` (fire-and-forget).

ANY failure raises :class:`~tai42_contract.channels.ChannelDeliveryError`, never
a silent drop. Slack reports most send failures as HTTP 200 with
``{"ok": false, "error": …}``, so success is decided by the JSON ``ok`` field,
not the HTTP status. ``notify`` shares recipient resolution and send validation
but posts a plain message (no correlation, no deadline, no reply) and returns the
posted ``ts``. A ``notify`` carrying ``sender_identity`` must name this
deployment's single bot identity (``CHANNEL_SLACK_BOT_USER_ID``) or it is refused.

The recipient allowlist governs ask_user deliveries; a bridge reply goes solely to
the conversation that initiated it, resolved by the bridge, not the allowlist.

Tier-1 (``confirm``/``external``) skip correlation: their answers travel through
the callback door directly (delivered with the callback URL as a plain link),
never a typed chat reply. Only ``text`` and ``select`` take the Tier-2
typed-reply path.

``form`` questions (``supports_form_delivery``) take a third path: the message
carries a button that opens a Block Kit modal, and the answer arrives as a
``view_submission`` on the interactivity door — the form record reserved here (in
Redis, before the send) holds the schema and callback URL that door needs.
"""

from __future__ import annotations

from typing import Any, ClassVar

import httpx
from pydantic import SecretStr
from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification
from tai42_kit.settings import require, require_secret

from tai42_channel_slack.client import slack_http
from tai42_channel_slack.correlation import (
    delete_form_record,
    remaining_seconds,
    store_correlation,
    store_form_record,
)
from tai42_channel_slack.forms import FormSchemaError, build_message_blocks, build_modal_view
from tai42_channel_slack.settings import SlackSettings, slack_settings

# Answered through the callback door directly (tappable plain link), never a typed
# thread reply: confirm (door records a bool from the tap) and external (structured POST).
_TIER1_FORMATS = frozenset({"confirm", "external"})


def _require_for_delivery[T](value: T | None, env_name: str) -> T:
    """The configured value, or :class:`ChannelDeliveryError` naming the env var
    (a config gap on the deliver/notify path is a delivery failure; wraps
    ``require``, raised before any network call)."""
    try:
        return require(value, "the slack channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _require_secret_for_delivery(value: SecretStr | None, env_name: str) -> str:
    """The secret's plaintext, or :class:`ChannelDeliveryError` on unset/EMPTY
    (fail closed; wraps ``require_secret``, message names only the env var)."""
    try:
        return require_secret(value, "the slack channel", env_name)
    except ValueError as exc:
        raise ChannelDeliveryError(str(exc)) from exc


def _render_text(delivery: ChannelDelivery) -> str:
    """The question as Slack message text. Tier-2 (``text``/``select``) carries the
    reply-in-thread instruction (the thread is the correlation key); Tier-1
    (``confirm``/``external``) carries the callback URL as a plain link. ``select``
    enumerates its options inline; the answer is validated at the callback door."""
    lines = [delivery.question]
    if delivery.answer_format in _TIER1_FORMATS:
        lines.append(f"Answer here: {delivery.callback_url}")
        lines.append(f"Deadline: {delivery.timeout_at.isoformat()}")
        return "\n".join(lines)
    if delivery.options:
        lines.append("Options: " + ", ".join(delivery.options))
    lines.append(f"Reply in this thread. Deadline: {delivery.timeout_at.isoformat()}")
    return "\n".join(lines)


def _resolve_recipient(settings: SlackSettings, requested: str | None) -> str:
    """The conversation id to send to.

    A caller-requested recipient must be on the operator allowlist or is refused
    loudly (fail CLOSED; empty allowlist rejects every request); no requested
    recipient uses the operator-set default (no allowlist check). Missing config
    raises :class:`ChannelDeliveryError` naming the env var.
    """
    if requested is None:
        return _require_for_delivery(settings.default_recipient, "CHANNEL_SLACK_DEFAULT_RECIPIENT")
    if requested not in set(settings.allowed_recipients):
        raise ChannelDeliveryError(
            f"recipient {requested!r} is not on CHANNEL_SLACK_ALLOWED_RECIPIENTS; refusing to send"
        )
    return requested


async def _post_message(
    token: str, target: str, text: str, blocks: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """POST one ``chat.postMessage`` and return its validated JSON body.

    ``blocks`` (Block Kit) rides alongside ``text``; ``text`` is kept as the
    notification fallback Slack requires even when blocks carry the content. Any
    failure — transport error, non-200 status, non-JSON body, or ``ok`` not true —
    raises :class:`ChannelDeliveryError`. Slack answers HTTP 200 even for errors,
    so the JSON ``ok`` field is the ONLY success signal.
    """
    payload: dict[str, Any] = {"channel": target, "text": text}
    if blocks is not None:
        payload["blocks"] = blocks
    try:
        async with slack_http() as client:
            response = await client.post(
                f"{slack_settings().api_base_url}/chat.postMessage",
                headers={"Authorization": f"Bearer {token}"},
                json=payload,
            )
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"chat.postMessage transport failure: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(f"chat.postMessage returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError("chat.postMessage returned a non-JSON body") from exc
    if body.get("ok") is not True:
        raise ChannelDeliveryError(f"chat.postMessage failed: {body.get('error', 'unknown error')}")
    return body


async def open_modal_view(trigger_id: str, view: dict[str, Any]) -> None:
    """POST one ``views.open`` for a form question's modal, or raise.

    Called inline from the interactivity door on a ``tai42_form_open`` click, with
    the ``block_actions`` ``trigger_id`` (valid ~3 s). Any failure — transport
    error, non-200 status, non-JSON body, or ``ok`` not true — raises
    :class:`ChannelDeliveryError`; the door lets it surface as a loud 500 so Slack
    reports the failure to open. Settings (bot token, api base) read fresh.
    """
    settings = slack_settings()
    token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
    try:
        async with slack_http() as client:
            response = await client.post(
                f"{settings.api_base_url}/views.open",
                headers={"Authorization": f"Bearer {token}"},
                json={"trigger_id": trigger_id, "view": view},
            )
    except httpx.HTTPError as exc:
        raise ChannelDeliveryError(f"views.open transport failure: {exc}") from exc
    if response.status_code != 200:
        raise ChannelDeliveryError(f"views.open returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise ChannelDeliveryError("views.open returned a non-JSON body") from exc
    if body.get("ok") is not True:
        raise ChannelDeliveryError(f"views.open failed: {body.get('error', 'unknown error')}")


async def _deliver_form(token: str, target: str, delivery: ChannelDelivery) -> None:
    """Deliver a ``form`` question: post a section + a button whose click opens the
    Block Kit modal, after reserving the form's state.

    The full modal view the click will build is composed here (and discarded)
    BEFORE any Redis or network work — so an unmappable schema OR a modal past a
    Slack cap (including the 100-block cap the click enforces) is a
    :class:`ChannelDeliveryError` naming the fault, and nothing is stored or sent;
    an uncompletable form is never delivered only to 500 at the click. The form
    record is written (reserve-before-send) and released if the send fails, so a
    failed post never leaves a live record with no message behind it.
    """
    schema = delivery.schema
    if not schema:
        # The contract guarantees a non-empty schema for a form; a gap here is a
        # delivery failure, not a silent plain-text send.
        raise ChannelDeliveryError("form answer_format requires a non-empty schema")
    try:
        # Compose the exact modal the click will build, discarding it — same
        # single source of truth, so every cap it enforces is enforced here.
        build_modal_view(delivery.interaction_id, delivery.question, schema)
        message_blocks = build_message_blocks(delivery.question, delivery.interaction_id)
    except FormSchemaError as exc:
        raise ChannelDeliveryError(str(exc)) from exc
    await store_form_record(
        delivery.interaction_id, delivery.callback_url, schema, delivery.question, delivery.timeout_at
    )
    try:
        await _post_message(token, target, delivery.question, blocks=message_blocks)
    except ChannelDeliveryError:
        # Reserve-before-send: a failed post releases the reservation so no live
        # form record dangles without a message.
        await delete_form_record(delivery.interaction_id)
        raise


class SlackChannel:
    """Registered under ``"slack"``; satisfies ``tai42_contract.channels.Channel``."""

    supports_form_delivery: ClassVar[bool] = True

    async def deliver(self, delivery: ChannelDelivery) -> None:
        # Settings read fresh each call so a rotated token or changed recipient
        # policy takes effect on the next question. Missing config is a delivery
        # failure: ChannelDeliveryError naming the env var, before any network call.
        settings = slack_settings()
        token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
        target = _resolve_recipient(settings, delivery.recipient)
        if remaining_seconds(delivery.timeout_at) <= 0:
            # Never post a question whose budget is already spent.
            raise ChannelDeliveryError(
                f"question budget already expired (timeout_at={delivery.timeout_at.isoformat()})"
            )
        if delivery.answer_format == "form":
            await _deliver_form(token, target, delivery)
            return
        body = await _post_message(token, target, _render_text(delivery))
        if delivery.answer_format in _TIER1_FORMATS:
            # Tier-1 (confirm/external): the plain link IS the answer path — no
            # correlation entry, no threaded reply expected.
            return
        ts = body.get("ts")
        if not isinstance(ts, str) or not ts:
            raise ChannelDeliveryError("chat.postMessage ok response carried no ts")
        await store_correlation(ts, delivery.callback_url, delivery.timeout_at)

    async def notify(self, notification: ChannelNotification) -> list[str]:
        """Post one plain fire-and-forget message via ``chat.postMessage``, returning
        the posted message ``[ts]``.

        No correlation, no deadline, no reply — the text is sent as-is. Without
        ``sender_identity`` the recipient follows ``deliver``'s allowlist policy;
        with it set (a bridge reply) it must equal this deployment's single bot
        identity — else refused with
        :class:`~tai42_contract.channels.ChannelDeliveryError`, never a send from the
        wrong face — and the recipient is the initiating conversation verbatim,
        allowlist bypassed. A return means Slack ACCEPTED it (not that a human saw
        it); any failure raises ``ChannelDeliveryError``. Settings read fresh.
        """
        settings = slack_settings()
        token = _require_secret_for_delivery(settings.bot_token, "CHANNEL_SLACK_BOT_TOKEN")
        if notification.sender_identity is not None:
            identity = _require_for_delivery(settings.bot_user_id, "CHANNEL_SLACK_BOT_USER_ID")
            if notification.sender_identity != identity:
                raise ChannelDeliveryError(
                    f"sender_identity {notification.sender_identity!r} is not this channel's identity {identity!r}"
                )
            # Bridge reply: send to the initiating conversation verbatim, allowlist bypassed.
            target = (
                notification.recipient
                if notification.recipient is not None
                else _require_for_delivery(settings.default_recipient, "CHANNEL_SLACK_DEFAULT_RECIPIENT")
            )
        else:
            target = _resolve_recipient(settings, notification.recipient)
        body = await _post_message(token, target, notification.message)
        ts = body.get("ts")
        if not isinstance(ts, str) or not ts:
            raise ChannelDeliveryError("chat.postMessage ok response carried no ts")
        return [ts]
