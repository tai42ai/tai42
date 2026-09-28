"""The public inbound door Telegram's webhook POSTs updates to.

``POST /api/channels/telegram/inbound`` (declared ``public: true`` in
``tai-plugin.yml``): verify the ``X-Telegram-Bot-Api-Secret-Token`` header against
the configured webhook secret
(constant-time over sha256 digests; FAIL CLOSED on missing config). A ForceReply
reply — text OR a media member — from a configured recipient chat whose question is
still pending resolves that ask, forwarded to its callback door; a media reply resolves
it with the caption/placeholder text and the parity ``media_*`` params (the answer ladder
carries no typed attachment). Every other user message — a fresh text or media member
(photo/document/audio/voice/video/video_note/animation/sticker), and a ForceReply reply
whose question has expired — is a bridge message handed to the conversation bridge keyed by
this bot's numeric id and the chat id. A content this channel recognises but cannot map to a
turn (poll/dice/venue/contact/location/game) gets the one generic refusal notice instead of a
silent drop.

Transport authentication runs first on every path; the recipient allowlist and
the reply shape gate only the ask path, never the bridge.

Telegram redelivers until a 2xx, so each branch picks its status deliberately:
verification failures deny (401/500), an unrouted or out-of-scope update acks
(200, logged), and a transient failure raises (500) so redelivery is the recovery.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Literal, NamedTuple

from pydantic import BaseModel
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_contract.conversations import InboundRejectionReason
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body

from tai42_channel_telegram.client import answer_callback_query
from tai42_channel_telegram.correlation import get_options
from tai42_channel_telegram.inbound_bridge import (
    _bridge,
    _ignored,
    _is_recipient_chat,
    _misconfigured,
    _our_identity,
    _reply_params,
    _resolve_answer,
    _resolve_or_bridge,
)
from tai42_channel_telegram.inbound_media import _bridge_media, _MediaMessage, _resolve_media
from tai42_channel_telegram.settings import TelegramSettings, telegram_settings

logger = logging.getLogger(__name__)

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 constant identifier, not a secret value
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024


class StatusAck(BaseModel):
    """The webhook's ack status naming which branch handled the update."""

    status: Literal["accepted", "forwarded", "ignored", "rejected"]


def _denied() -> JSONResponse:
    # One constant deny for every verification failure — no missing-vs-wrong oracle.
    return JSONResponse({"error": "verification failed"}, status_code=401)


async def _resolve_callback(settings: TelegramSettings, update: dict[str, object]) -> Response:
    """Resolve an inline-keyboard button tap (a ``callback_query`` update).

    The tapped button's ``callback_data`` is the option's wire token; the anchor
    ``message_id`` the query reports keys the side record holding that message's option
    records, so the token maps back to the exact :class:`StoredOption` — its text (submitted
    as the turn) and its author-set id / description (carried as ``params.reply_id`` /
    ``params.reply_description`` on a BRIDGED tap). A select / suggested-reply tap from a
    recipient chat resolves through the shared ladder (like a typed reply); a notify-option
    tap (no pending ask — a correlation miss) enters the conversation as a visitor message
    via the bridge. A tap for a message with no live option record (an expired ask, a stale
    keyboard), or one whose token matches no record, is acked and ignored.

    The callback query is answered first (best-effort) so the button's spinner clears
    regardless of the routing outcome; a failure there is logged, never raised (an
    unanswered callback must not 5xx the webhook and force a redelivery).
    """
    callback_query = update.get("callback_query")
    if not isinstance(callback_query, dict):  # defensive — the caller checked this
        return _ignored("update carries no callback_query")

    query_id = callback_query.get("id")
    if isinstance(query_id, str) and query_id:
        try:
            await answer_callback_query(query_id)
        except ChannelDeliveryError as exc:
            logger.warning("telegram inbound: answerCallbackQuery for %s failed: %s", query_id, exc)

    message = callback_query.get("message")
    chat = message.get("chat") if isinstance(message, dict) else None
    message_id = message.get("message_id") if isinstance(message, dict) else None
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    data = callback_query.get("data")
    if not isinstance(message_id, int) or not isinstance(chat, dict) or not isinstance(chat_id, int):
        return _ignored("callback query carries no anchor message/chat id")
    if not isinstance(data, str) or not data:
        return _ignored("callback query carries no callback_data token")

    options = await get_options(str(chat_id), str(message_id))
    if options is None:
        return _ignored("callback query for a message with no live option record")
    matched = next((option for option in options if option.callback_data == data), None)
    if matched is None:
        return _ignored("callback query token matches no live option")
    text = matched.text
    # Opaque tap enrichment carried onto a BRIDGED turn (never surfaced on a clean answer
    # forward, whose seam takes only the answer): the author-set id and any row description.
    reply_params = _reply_params(matched)

    # A recipient-chat tap on a select/suggested-reply ask resolves via the ladder; a
    # miss (a notify option, or an expired ask) falls through to the bridge — the same
    # split the typed-reply path takes, keyed on the anchor message id.
    if _is_recipient_chat(chat, settings):
        resolved = await _resolve_answer(settings, message_id, chat_id, text, update, params=reply_params)
        if resolved is not None:
            return resolved
    return await _bridge(settings, chat_id, text, update, params=reply_params)


def _verify_secret(request: Request, settings: TelegramSettings) -> Response | None:
    """Transport auth for the inbound door, FAILING CLOSED.

    Returns a 500 when the webhook secret is unconfigured, a constant 401 deny when
    the ``X-Telegram-Bot-Api-Secret-Token`` header is absent or wrong, or ``None``
    when the request is authenticated.
    """
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
    return None


class _UpdateRejectedError(Exception):
    """A bounded-read or parse failure carrying the webhook response to return."""

    def __init__(self, response: Response) -> None:
        super().__init__()
        self.response = response


async def _read_update(request: Request) -> dict[str, object]:
    """Read the bounded request body and parse it into an update dict.

    Raises :class:`_UpdateRejectedError` carrying a 413 (over the byte cap), or a 400
    (unparseable body, or a body that is not a JSON object).
    """
    try:
        body = await read_bounded_body(request, _MAX_BODY_BYTES)
    except RequestBodyTooLargeError:
        raise _UpdateRejectedError(JSONResponse({"error": "payload too large"}, status_code=413)) from None
    try:
        update = json.loads(body)
    except ValueError:
        raise _UpdateRejectedError(JSONResponse({"error": "body must be a JSON object"}, status_code=400)) from None
    if not isinstance(update, dict):
        raise _UpdateRejectedError(JSONResponse({"error": "body must be a JSON object"}, status_code=400))
    return update


class _Bridgeable(NamedTuple):
    """A text message that becomes a turn: its chat and numeric chat id, and the turn text."""

    chat: dict[str, object]
    chat_id: int
    text: str


class _Unsupported(NamedTuple):
    """A content this channel recognises but cannot map to a turn — the vendor member word refused."""

    chat_id: int
    kind: str


# Content Telegram can send that this channel recognises but does not map to a media kind —
# each routes to the one generic unsupported reply + event, never a silent drop.
_UNSUPPORTED_MEMBERS = ("poll", "dice", "venue", "contact", "location", "game")


def _message_fields(message: dict[str, object]) -> _Bridgeable | _MediaMessage | _Unsupported | Response:
    """Classify a message into a text turn, a media message, an unsupported-content refusal, or an ack.

    A text message bridges its ``text``; a media message (photo/document/audio/voice/video/
    video_note/animation/sticker) returns :class:`_MediaMessage` so the door fetches the bytes,
    ingests them into served media, and bridges ONE turn with the typed attachment. A content
    this channel recognises but cannot map (poll/dice/venue/contact/location/game) returns
    :class:`_Unsupported` so the door sends the one generic refusal. A message with no chat,
    no numeric chat id, or no recognised content returns an acked-ignored 200.
    """
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return _ignored("message carries no chat id")
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return _ignored("message carries no chat id")
    text = message.get("text")
    if isinstance(text, str):
        return _Bridgeable(chat, chat_id, text)
    member = _resolve_media(message)
    if member is not None:
        raw_caption = message.get("caption")
        caption = raw_caption.strip() if isinstance(raw_caption, str) and raw_caption.strip() else None
        return _MediaMessage(chat=chat, chat_id=chat_id, member=member, caption=caption)
    for name in _UNSUPPORTED_MEMBERS:
        if name in message:
            return _Unsupported(chat_id, name)
    return _ignored("message carries no bridgeable content")


async def _reject_unsupported(settings: TelegramSettings, chat_id: int, kind: str) -> Response:
    """Send the one generic refusal for a recognised-but-unmappable content, then ack.

    Routes through the shared ``notify_inbound_rejected`` chokepoint (the single participant
    notice + operator event) rather than silently dropping the update, then acks so Telegram
    stops redelivering it.
    """
    our_identity = _our_identity(settings)
    if isinstance(our_identity, JSONResponse):
        return our_identity
    await tai42_app.conversations.notify_inbound_rejected(
        channel_id="telegram",
        recipient=str(chat_id),
        sender_identity=our_identity,
        kind=kind,
        reason=InboundRejectionReason.UNSUPPORTED_TYPE,
    )
    return _ignored("unsupported content")


@tai42_app.http.custom_route(
    "/inbound",
    methods=["POST"],
    summary="Telegram channel inbound webhook",
    tags=["channels"],
    response_model=StatusAck,
)
async def inbound(request: Request) -> Response:
    """Receive a Telegram webhook update, resolve a pending ask or bridge the message.

    Transport auth runs first. A ForceReply reply from a recipient chat matching a
    pending question is forwarded to its callback door; any other text message — or
    an expired reply — is bridged to the conversation route.
    """
    settings = telegram_settings()

    denied = _verify_secret(request, settings)
    if denied is not None:
        return denied

    try:
        update = await _read_update(request)
    except _UpdateRejectedError as rejected:
        return rejected.response

    # An inline-keyboard button tap arrives as a callback_query, not a message: it
    # maps the tapped option's index back to its text and resolves/bridges it.
    if isinstance(update.get("callback_query"), dict):
        return await _resolve_callback(settings, update)

    message = update.get("message")
    if not isinstance(message, dict):
        return _ignored("update carries no message")
    fields = _message_fields(message)
    if isinstance(fields, Response):
        return fields
    if isinstance(fields, _Unsupported):
        return await _reject_unsupported(settings, fields.chat_id, fields.kind)

    # A media message is fetched and ingested into served media FIRST, then routed by the same
    # reply-correlation the text path uses: a reply to a still-pending ask resolves it (text +
    # parity params), otherwise the typed attachment turn bridges.
    if isinstance(fields, _MediaMessage):
        return await _bridge_media(settings, fields, message, update)

    return await _resolve_or_bridge(settings, fields.chat, fields.chat_id, fields.text, message, update)
