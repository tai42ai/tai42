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
from tai42_contract.channels import ChannelDeliveryError
from tai42_contract.conversations import BlankInboundTextError
from tai42_kit.settings import require_secret

from tai42_channel_telegram.client import send_chat_action, telegram_http
from tai42_channel_telegram.correlation import clear_correlation, lookup_callback_url
from tai42_channel_telegram.settings import TelegramSettings, bot_numeric_id, telegram_settings

logger = logging.getLogger(__name__)

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024


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


async def _forward_answer(
    message_id: int,
    callback_url: str,
    settings: TelegramSettings,
    chat_id: int,
    text: str,
    update: dict[str, object],
) -> Response:
    """Forward a correlated ForceReply answer to its callback door and map the
    door's status to this webhook's ack. Transport errors propagate (-> 500).

    ``settings``/``chat_id``/``text``/``update`` are the bridge context so a terminal
    404 (the interaction is gone) bridges the reply into the conversation rather than
    dropping it — ``_bridge`` sets ``provider_message_id`` = the update id, so a
    Telegram redelivery of the same inbound dedupes at the conversation seam."""
    async with telegram_http() as client:
        forwarded = await client.post(callback_url, json={"answer": text})

    if forwarded.status_code == 200:
        await clear_correlation(message_id)
        return JSONResponse({"data": {"status": "forwarded"}}, status_code=200)
    # 404 is the ONE terminal callback status: the ticket is gone and retrying the
    # same answer can never succeed. (A duplicate forward resolves idempotently to 200.)
    if forwarded.status_code == 404:
        logger.warning(
            "telegram inbound: callback door returned terminal HTTP 404 for message_id=%s; the interaction "
            "is gone — bridging the reply into the conversation instead of dropping it",
            message_id,
        )
        await clear_correlation(message_id)
        return await _bridge(settings, chat_id, text, update)
    if forwarded.status_code == 400:
        logger.warning(
            "telegram inbound: callback door rejected the answer for message_id=%s (400); "
            "correlation kept so the human can reply again",
            message_id,
        )
        return JSONResponse({"data": {"status": "rejected"}}, status_code=200)
    raise RuntimeError(
        f"interaction callback returned HTTP {forwarded.status_code} for message_id={message_id}; "
        f"failing the webhook delivery so Telegram redelivers"
    )


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
    # still-pending question; an expired match falls through to the bridge.
    reply_to = message.get("reply_to_message")
    replied_id = reply_to.get("message_id") if isinstance(reply_to, dict) else None
    if isinstance(replied_id, int) and _is_recipient_chat(chat, settings):
        callback_url = await lookup_callback_url(replied_id)
        if callback_url is not None:
            return await _forward_answer(replied_id, callback_url, settings, chat_id, text, update)

    return await _bridge(settings, chat_id, text, update)
