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
this bot's numeric id and the chat id. A text that is blank gets the one unreadable notice; any
other content a user sends that this channel has no reader for (a labelled member such as
poll/dice/venue/contact/location/game, or a member it does not name) gets the one generic
unsupported notice instead of a silent drop. A service message (a chat event, not the user's
message) is acked and ignored.

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
    _reply_to_message_id,
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


def _tapped_label(message: dict[str, object], data: str) -> str | None:
    """The label of the button carrying ``data`` in ``message``'s inline keyboard, or ``None``.

    Read from ``reply_markup.inline_keyboard`` (rows of buttons with ``text`` and
    ``callback_data``). ``None`` when the message carries no keyboard, no button carries the
    token, or that button's label is blank.
    """
    markup = message.get("reply_markup")
    rows = markup.get("inline_keyboard") if isinstance(markup, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, list):
            continue
        for button in row:
            if isinstance(button, dict) and button.get("callback_data") == data:
                text = button.get("text")
                return text if isinstance(text, str) and text.strip() else None
    return None


async def _bridge_stale_tap(
    settings: TelegramSettings,
    chat_id: int,
    message: dict[str, object],
    data: str,
    update: dict[str, object],
    tapped_message_id: str,
) -> Response:
    """Bridge a tap whose token no live option holds as the user's message carrying the tapped label.

    The tap answers nothing, so it never reaches the answer ladder and never claims or changes a
    live ask. Its label is read off the query's own keyboard and bridged as a reply to the
    keyboard's message, with no params (no option record, no author-set id or description). A
    query that carries no such label gets the one choice-unavailable notice.
    """
    label = _tapped_label(message, data)
    if label is None:
        return await _notify_rejected(settings, chat_id, "callback_query", InboundRejectionReason.CHOICE_UNAVAILABLE)
    return await _bridge(
        settings,
        chat_id,
        label,
        update,
        medium_message_id=None,
        reply_to_medium_message_id=tapped_message_id,
    )


async def _notify_rejected(
    settings: TelegramSettings, chat_id: int, kind: str, reason: InboundRejectionReason
) -> Response:
    """Send the user the one notice for an inbound content that cannot become a turn, then ack.

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
        reason=reason,
    )
    return _ignored(f"inbound {kind} rejected: {reason}")


async def _resolve_callback(settings: TelegramSettings, update: dict[str, object]) -> Response:
    """Resolve an inline-keyboard button tap (a ``callback_query`` update).

    The tapped button's ``callback_data`` is the option's wire token; the anchor
    ``message_id`` the query reports keys the side record holding that message's option
    records, so the token maps back to the exact :class:`StoredOption` — its text (submitted
    as the turn) and its author-set id / description (carried as ``params.reply_id`` /
    ``params.reply_description`` on a BRIDGED tap). A select / suggested-reply tap from a
    recipient chat resolves through the shared ladder (like a typed reply); a notify-option
    tap (no pending ask — a correlation miss) enters the conversation as a visitor message
    via the bridge. A tap whose token no live option record holds (an expired ask, a stale
    keyboard) answers nothing and claims no ask: the tapped button's label, read off the
    query's own keyboard, bridges as the user's message with no params; when the query
    carries no such label (an inaccessible message has no keyboard) the user gets the one
    choice-unavailable notice.

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
    if not isinstance(message, dict):
        return _ignored("callback query carries no anchor message/chat id")
    chat = message.get("chat")
    message_id = message.get("message_id")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    data = callback_query.get("data")
    if not isinstance(message_id, int) or not isinstance(chat, dict) or not isinstance(chat_id, int):
        return _ignored("callback query carries no anchor message/chat id")
    if not isinstance(data, str) or not data:
        return _ignored("callback query carries no callback_data token")

    # A tap is a reply to the message holding the keyboard, in the tapping chat.
    tapped_message_id = str(message_id)
    options = await get_options(str(chat_id), str(message_id))
    matched = None if options is None else next((option for option in options if option.callback_data == data), None)
    if matched is None:
        return await _bridge_stale_tap(settings, chat_id, message, data, update, tapped_message_id)
    text = matched.text
    # Opaque tap enrichment carried onto a BRIDGED turn (never surfaced on a clean answer
    # forward, whose seam takes only the answer): the author-set id and any row description.
    reply_params = _reply_params(matched)

    # A recipient-chat tap on a select/suggested-reply ask resolves via the ladder; a
    # miss (a notify option, or an expired ask) falls through to the bridge — the same
    # split the typed-reply path takes, keyed on the anchor message id.
    if _is_recipient_chat(chat, settings):
        resolved = await _resolve_answer(
            settings,
            message_id,
            chat_id,
            text,
            update,
            medium_message_id=None,
            reply_to_medium_message_id=tapped_message_id,
            params=reply_params,
        )
        if resolved is not None:
            return resolved
    return await _bridge(
        settings,
        chat_id,
        text,
        update,
        medium_message_id=None,
        reply_to_medium_message_id=tapped_message_id,
        params=reply_params,
    )


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


class _Refused(NamedTuple):
    """A message this channel refuses with one notice: its chat id, the content kind, and the reason."""

    chat_id: int
    kind: str
    reason: InboundRejectionReason


# The Bot API ``Message`` content members a user can send or forward that this channel recognises
# but has no reader for, each mapped to the neutral content label the rejection names.
_CONTENT_LABELS: dict[str, str] = {
    "poll": "poll",
    "dice": "dice",
    "venue": "venue",
    "contact": "contact",
    "location": "location",
    "game": "game",
    "story": "story",
    "paid_media": "paid_media",
    "checklist": "checklist",
    "giveaway": "giveaway",
    "giveaway_winners": "giveaway_winners",
    "rich_message": "rich_message",
}

# The Bot API ``Message`` members this channel treats as a chat event, not the user's message:
# service-message members of the Bot API, plus the chat-state members (membership, title, photo,
# migration, pin, login). A message carrying one is acked and ignored; a service member not
# listed here falls to the ``other`` default.
_SERVICE_MEMBERS: frozenset[str] = frozenset(
    {
        "new_chat_members",
        "left_chat_member",
        "chat_owner_left",
        "chat_owner_changed",
        "new_chat_title",
        "new_chat_photo",
        "delete_chat_photo",
        "group_chat_created",
        "supergroup_chat_created",
        "channel_chat_created",
        "message_auto_delete_timer_changed",
        "migrate_to_chat_id",
        "migrate_from_chat_id",
        "pinned_message",
        "users_shared",
        "chat_shared",
        "gift",
        "unique_gift",
        "gift_upgrade_sent",
        "connected_website",
        "write_access_allowed",
        "proximity_alert_triggered",
        "boost_added",
        "chat_background_set",
        "checklist_tasks_done",
        "checklist_tasks_added",
        "community_chat_added",
        "community_chat_joined",
        "community_chat_removed",
        "direct_message_price_changed",
        "forum_topic_created",
        "forum_topic_edited",
        "forum_topic_closed",
        "forum_topic_reopened",
        "general_forum_topic_hidden",
        "general_forum_topic_unhidden",
        "giveaway_created",
        "giveaway_completed",
        "managed_bot_created",
        "paid_message_price_changed",
        "paid_messages_waived",
        "poll_option_added",
        "poll_option_deleted",
        "suggested_post_approved",
        "suggested_post_approval_failed",
        "suggested_post_declined",
        "suggested_post_paid",
        "ton_wallet_transfer",
        "video_chat_scheduled",
        "video_chat_started",
        "video_chat_ended",
        "video_chat_participants_invited",
        "web_app_data",
    }
)


def _message_fields(message: dict[str, object]) -> _Bridgeable | _MediaMessage | _Refused | Response:
    """Read a message into a text turn, a media message, a refusal, or an ack.

    A non-blank text bridges its ``text``; a blank one is refused as unreadable (kind ``text``).
    A media message (photo/document/audio/voice/video/video_note/animation/sticker) returns
    :class:`_MediaMessage` so the door fetches the bytes, ingests them into served media, and
    bridges ONE turn with the typed attachment. A member of ``_CONTENT_LABELS`` is refused as
    unsupported under its label; a member of ``_SERVICE_MEMBERS`` is acked and ignored. Any
    other message — a content member this channel does not name, or no content member at all,
    which is how the Bot API delivers a content type it does not model — is refused as
    unsupported (kind ``other``) with a WARNING naming the members it carried. A message with no
    chat or no numeric chat id is acked and ignored.
    """
    chat = message.get("chat")
    if not isinstance(chat, dict):
        return _ignored("message carries no chat id")
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return _ignored("message carries no chat id")
    text = message.get("text")
    if isinstance(text, str):
        if not text.strip():
            return _Refused(chat_id, "text", InboundRejectionReason.UNREADABLE)
        return _Bridgeable(chat, chat_id, text)
    member = _resolve_media(message)
    if member is not None:
        raw_caption = message.get("caption")
        caption = raw_caption.strip() if isinstance(raw_caption, str) and raw_caption.strip() else None
        return _MediaMessage(
            chat=chat,
            chat_id=chat_id,
            member=member,
            caption=caption,
            reply_to_medium_message_id=_reply_to_message_id(message),
        )
    for member_name, kind in _CONTENT_LABELS.items():
        if member_name in message:
            return _Refused(chat_id, kind, InboundRejectionReason.UNSUPPORTED_TYPE)
    if not _SERVICE_MEMBERS.isdisjoint(message):
        return _ignored("service message")
    logger.warning(
        "telegram inbound for chat_id=%s carries no content this channel reads (members: %s); "
        "sending the unsupported notice",
        chat_id,
        sorted(message),
    )
    return _Refused(chat_id, "other", InboundRejectionReason.UNSUPPORTED_TYPE)


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
    if isinstance(fields, _Refused):
        return await _notify_rejected(settings, fields.chat_id, fields.kind, fields.reason)

    # A media message is fetched and ingested into served media FIRST, then routed by the same
    # reply-correlation the text path uses: a reply to a still-pending ask resolves it (text +
    # parity params), otherwise the typed attachment turn bridges.
    if isinstance(fields, _MediaMessage):
        return await _bridge_media(settings, fields, message, update)

    return await _resolve_or_bridge(
        settings,
        fields.chat,
        fields.chat_id,
        fields.text,
        message,
        update,
        reply_to_medium_message_id=_reply_to_message_id(message),
    )
