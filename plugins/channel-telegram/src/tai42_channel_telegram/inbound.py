"""The public inbound door Telegram's webhook POSTs updates to.

``POST /api/channels/telegram/inbound`` (declared ``public: true`` in
``tai-plugin.yml``): verify the ``X-Telegram-Bot-Api-Secret-Token`` header against
the configured webhook secret
(constant-time over sha256 digests; FAIL CLOSED on missing config). A ForceReply
reply from a configured recipient chat whose question is still pending resolves
that ask — forwarded to its callback door. Every other user text message, and a
ForceReply reply whose question has expired, is a bridge message handed to the
conversation bridge keyed by this bot's numeric id and the chat id.

Transport authentication runs first on every path; the recipient allowlist and
the reply shape gate only the ask_user path, never the bridge.

Telegram redelivers until a 2xx, so each branch picks its status deliberately:
verification failures deny (401/500), an unrouted or out-of-scope update acks
(200, logged), and a transient failure raises (500) so redelivery is the recovery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError, InboundAnswerOutcome, InboundBridge
from tai42_contract.conversations import BlankInboundTextError
from tai42_kit.settings import require_secret

from tai42_channel_telegram.client import send_chat_action
from tai42_channel_telegram.correlation import telegram_correlation_store
from tai42_channel_telegram.settings import TelegramSettings, bot_numeric_id, telegram_settings

logger = logging.getLogger(__name__)

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024

# The shared ladder's outcome -> this webhook's ``{"data": {"status": ...}}`` ack. The
# statuses match the wire the hand-rolled ladder answered: a resolved answer forwards,
# a kept re-answerable ask reads "rejected", a bridged (gone/hard-mismatch) reply reads
# "accepted" — the same string a fresh-turn bridge returns. NO_CORRELATION is absent:
# the caller bridges that miss and returns the bridge's own ack.
_ACK_STATUS = {
    InboundAnswerOutcome.FORWARDED: "forwarded",
    InboundAnswerOutcome.RETRY_KEPT: "rejected",
    InboundAnswerOutcome.BRIDGED: "accepted",
}


class _PayloadTooLarge(Exception):
    """The inbound body exceeded ``_MAX_BODY_BYTES`` -> 413."""


async def _read_bounded_body(request: Request, cap: int) -> bytes:
    """Read the body on ACTUAL bytes, never a client ``Content-Length``. Raise
    ``_PayloadTooLarge`` the moment the stream crosses ``cap``."""
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            raise _PayloadTooLarge("request body exceeds the configured cap")
        chunks.append(chunk)
    return b"".join(chunks)


def _misconfigured(env_name: str) -> JSONResponse:
    logger.error("telegram inbound: %s is unset or malformed; failing closed", env_name)
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


def _denied() -> JSONResponse:
    # One constant deny for every verification failure — no missing-vs-wrong oracle.
    return JSONResponse({"error": "verification failed"}, status_code=401)


def _ignored(reason: str) -> JSONResponse:
    logger.info("telegram inbound: update ignored: %s", reason)
    return JSONResponse({"data": {"status": "ignored"}}, status_code=200)


def _is_recipient_chat(chat: dict[str, object], settings: TelegramSettings) -> bool:
    """Whether ``chat`` is a configured recipient — matched by numeric id or
    ``@username``. Only these chats may ANSWER an ask_user question."""
    recipient_chats = set(settings.allowed_recipients)
    if settings.default_recipient is not None:
        recipient_chats.add(settings.default_recipient)
    username = chat.get("username")
    return str(chat.get("id")) in recipient_chats or (isinstance(username, str) and f"@{username}" in recipient_chats)


async def _resolve_answer(
    settings: TelegramSettings,
    replied_id: int,
    chat_id: int,
    text: str,
    update: dict[str, object],
) -> Response | None:
    """Resolve a ForceReply answer against its pending ask via the ONE shared ladder.

    The correlation key is the anchor message's id; the answer is the reply text
    verbatim. The ladder forwards to the door and interprets the outcome (release /
    keep-and-notify / bridge) over the plugin's :class:`CorrelationStore` — the plugin
    keeps only its transport ack. Returns the webhook ack for a resolved/kept/bridged
    outcome, or ``None`` on a correlation miss so the caller bridges the reply as a
    fresh turn (the ladder never bridges on a miss — the caller does, exactly as
    before). An :class:`AnswerForwardError` (401/413/5xx / transport fault) propagates
    to a 500 so Telegram redelivers and re-runs the ladder.

    ``update_id`` is the bridge's idempotency key and ``our_identity`` the bot's
    numeric id (a malformed token is a loud 500), both resolved up front so the
    :class:`InboundBridge` a 404/hard-mismatch bridge needs is ready before the call.
    """
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return JSONResponse({"error": "update carries no integer update_id"}, status_code=400)
    try:
        our_identity = bot_numeric_id(
            require_secret(settings.bot_token, "the telegram channel", "CHANNEL_TELEGRAM_BOT_TOKEN")
        )
    except ValueError:
        return _misconfigured("CHANNEL_TELEGRAM_BOT_TOKEN")

    result = await tai42_app.channels.handle_inbound_answer(
        channel_id="telegram",
        correlation_key=str(replied_id),
        answer=text,
        store=telegram_correlation_store,
        bridge=InboundBridge(
            channel_id="telegram",
            our_identity=our_identity,
            client_address=str(chat_id),
            # The provider attests the chat id, so it is both the conversation identity
            # and the party the turn cap holds accountable.
            cap_key=str(chat_id),
            provider_message_id=str(update_id),
            bridge_text=text,
        ),
    )
    if result.outcome is InboundAnswerOutcome.NO_CORRELATION:
        return None
    return JSONResponse({"data": {"status": _ACK_STATUS[result.outcome]}}, status_code=200)


async def _bridge(settings: TelegramSettings, chat_id: int, text: str, update: dict[str, object]) -> Response:
    """Hand an uncorrelated user message to the conversation bridge.

    ``our_identity`` is this bot's numeric id (malformed token -> loud 500);
    ``client_address`` is the numeric chat id; ``provider_message_id`` is the
    update id. No route bound or blank text -> ack + log; a transient failure
    propagates (500).
    """
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return JSONResponse({"error": "update carries no integer update_id"}, status_code=400)
    try:
        our_identity = bot_numeric_id(
            require_secret(settings.bot_token, "the telegram channel", "CHANNEL_TELEGRAM_BOT_TOKEN")
        )
    except ValueError:
        return _misconfigured("CHANNEL_TELEGRAM_BOT_TOKEN")

    try:
        await tai42_app.conversations.accept(
            channel="telegram",
            our_identity=our_identity,
            client_address=str(chat_id),
            # The provider attests the chat id, so it is both the conversation identity
            # and the party the turn cap holds accountable.
            cap_key=str(chat_id),
            text=text,
            provider_message_id=str(update_id),
        )
    except BlankInboundTextError:
        # A whitespace-only message is nothing to bridge — ack so Telegram stops
        # redelivering it.
        logger.warning("telegram inbound: blank text for chat_id=%s; ignoring", chat_id)
        return _ignored("blank message text")
    except LookupError:
        # No route bound for this identity — ack so Telegram stops redelivering a
        # permanently-unrouted address. A transient failure instead propagates
        # (-> 500) so Telegram redelivers rather than dropping the message.
        logger.warning("telegram inbound: no conversation route for chat_id=%s; ignoring", chat_id)
        return _ignored("no conversation route for this message")
    return JSONResponse({"data": {"status": "accepted"}}, status_code=200)


@tai42_app.http.custom_route(
    "/inbound",
    methods=["POST"],
    summary="Telegram channel inbound webhook",
    tags=["channels"],
    response_model=None,
)
async def inbound(request: Request) -> Response:
    """Receive a Telegram webhook update, resolve a pending ask or bridge the message.

    Transport auth runs first. A ForceReply reply from a recipient chat matching a
    pending question is forwarded to its callback door; any other text message — or
    an expired reply — is bridged to the conversation route.
    """
    settings = telegram_settings()
    configured = settings.webhook_secret.get_secret_value() if settings.webhook_secret else ""
    if not configured:
        return _misconfigured("CHANNEL_TELEGRAM_WEBHOOK_SECRET")

    provided = request.headers.get(_SECRET_HEADER)
    # Hash both sides before the constant-time compare so an unequal-length raw
    # input can't leak the secret's length; sha256 fixes both at 32 bytes.
    if provided is None or not hmac.compare_digest(
        hashlib.sha256(provided.encode()).digest(),
        hashlib.sha256(configured.encode()).digest(),
    ):
        return _denied()

    try:
        body = await _read_bounded_body(request, _MAX_BODY_BYTES)
    except _PayloadTooLarge:
        return JSONResponse({"error": "payload too large"}, status_code=413)
    try:
        update = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
    if not isinstance(update, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    message = update.get("message")
    if not isinstance(message, dict):
        return _ignored("update carries no message")
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return _ignored("message carries no chat id")
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return _ignored("message carries no chat id")
    text = message.get("text")
    if not isinstance(text, str):
        return _ignored("message carries no text (a media message is not bridgeable)")

    # Signal "working on it" the moment a processable message lands — a typing
    # action shown BEFORE the ask/bridge split so it covers both paths. A delivery
    # failure is logged, never raised: it must not fail the webhook (Telegram
    # would redeliver the whole update).
    try:
        await send_chat_action(chat_id, "typing")
    except ChannelDeliveryError as exc:
        logger.warning("telegram inbound: typing action for chat_id=%s failed: %s", chat_id, exc)

    # ask_user wins when a ForceReply reply from a recipient chat matches a
    # still-pending question; a correlation miss (expired/never-ours) falls through to
    # the bridge — the shared ladder returns NO_CORRELATION and the caller bridges.
    reply_to = message.get("reply_to_message")
    replied_id = reply_to.get("message_id") if isinstance(reply_to, dict) else None
    if isinstance(replied_id, int) and _is_recipient_chat(chat, settings):
        resolved = await _resolve_answer(settings, replied_id, chat_id, text, update)
        if resolved is not None:
            return resolved

    return await _bridge(settings, chat_id, text, update)
