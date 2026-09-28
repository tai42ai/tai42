"""The public inbound door Telegram's webhook POSTs updates to.

``POST /api/channels/telegram/inbound`` (declared ``public: true`` in
``tai-plugin.yml``): verify the ``X-Telegram-Bot-Api-Secret-Token`` header against
the configured webhook secret
(constant-time over sha256 digests; FAIL CLOSED on missing config). A ForceReply
reply from a configured recipient chat whose question is still pending resolves
that ask — forwarded to its callback door. Every other user message — text or a
media member (photo/document/audio/voice/video/video_note/animation/sticker) — and
a ForceReply reply whose question has expired, is a bridge message handed to the
conversation bridge keyed by this bot's numeric id and the chat id. A content this
channel recognises but cannot map to a turn (poll/dice/venue/contact/location/game)
gets the one generic refusal notice instead of a silent drop.

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
from tai42_contract.channels import ChannelDeliveryError, InboundAnswerOutcome, InboundBridge
from tai42_contract.conversations import (
    ENTRY_PARAM_VALUE_MAX_CHARS,
    BlankInboundTextError,
    InboundMediaKind,
    InboundRejectionReason,
    build_inbound_media_params,
    inbound_media_placeholder,
    validate_entry_params,
)
from tai42_contract.locale import InvalidLocaleError, normalize_optional_locale
from tai42_kit.net.request_body import RequestBodyTooLargeError, read_bounded_body
from tai42_kit.settings import require_secret

from tai42_channel_telegram.client import answer_callback_query, send_chat_action
from tai42_channel_telegram.correlation import (
    StoredOption,
    get_options,
    scoped_correlation_key,
    telegram_correlation_store,
)
from tai42_channel_telegram.settings import TelegramSettings, bot_numeric_id, telegram_settings

logger = logging.getLogger(__name__)

# Inbound entry-params vocabulary — the channel's PUBLIC contract for the opaque
# ``payload["params"]`` a channel-agnostic tool consumer reads. Params ride ONLY on the
# BRIDGE path (a tap that is not, or is no longer, an answer to a pending ask): a tap that
# ANSWERS forwards ``{"answer": …}`` to the callback door alongside these params, and the
# tap's token is already consumed there to select the option. The keys:
#
#   reply_id           — the AUTHOR-SET id of the tapped reply option / list row, echoed
#                        back so a consumer sees WHICH option was tapped, not just its label.
#                        Absent when the option carried no author-set id (a plain option, or
#                        a select/suggested-reply ask whose options carry none).
#   reply_description  — a tapped sectioned-row's secondary description line, when it had one.
#
# All values are transport-bounded by the contract (:func:`validate_entry_params`); a value
# over ``ENTRY_PARAM_VALUE_MAX_CHARS`` is dropped (never truncated), and in the rare event
# the aggregate still overflows a bound the whole set is dropped and the turn bridges without
# it — a participant message is never lost to a params bound.

_SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"  # noqa: S105 constant identifier, not a secret value
# Bound what an unauthenticated door reads into memory — loud 413, never truncation.
_MAX_BODY_BYTES = 1 * 1024 * 1024

# The shared ladder's outcome -> this webhook's ``{"data": {"status": ...}}`` ack. The
# statuses are this webhook's stable ack vocabulary: a resolved answer forwards,
# a kept re-answerable ask reads "rejected", and a bridged reply reads "accepted" — the
# same string a fresh-turn bridge returns, covering both a released bridge (gone ask /
# hard mismatch) and a bridge-policy digression that KEPT the ask (BRIDGED_KEPT).
# NO_CORRELATION is absent: the caller bridges that miss and returns the bridge's own ack.
_ACK_STATUS = {
    InboundAnswerOutcome.FORWARDED: "forwarded",
    InboundAnswerOutcome.RETRY_KEPT: "rejected",
    InboundAnswerOutcome.BRIDGED: "accepted",
    InboundAnswerOutcome.BRIDGED_KEPT: "accepted",
}


class StatusAck(BaseModel):
    """The webhook's ack status naming which branch handled the update."""

    status: Literal["accepted", "forwarded", "ignored", "rejected"]


def _misconfigured(env_name: str) -> JSONResponse:
    logger.error("telegram inbound: %s is unset or malformed; failing closed", env_name)
    return JSONResponse({"error": "channel misconfigured"}, status_code=500)


def _our_identity(settings: TelegramSettings) -> str | JSONResponse:
    """This bot's numeric id (the digits before the ``:`` in the token), or a loud 500.

    A malformed/unset token yields the misconfigured 500 — the same fail-closed response
    every door path returns when the identity it must reply from cannot be resolved.
    """
    try:
        return bot_numeric_id(require_secret(settings.bot_token, "the telegram channel", "CHANNEL_TELEGRAM_BOT_TOKEN"))
    except ValueError:
        return _misconfigured("CHANNEL_TELEGRAM_BOT_TOKEN")


def _denied() -> JSONResponse:
    # One constant deny for every verification failure — no missing-vs-wrong oracle.
    return JSONResponse({"error": "verification failed"}, status_code=401)


def _ignored(reason: str) -> JSONResponse:
    logger.info("telegram inbound: update ignored: %s", reason)
    return JSONResponse({"data": {"status": "ignored"}}, status_code=200)


def _put_param(params: dict[str, str], key: str, value: str | None) -> None:
    """Add ``key`` iff ``value`` is a non-empty string within the contract's per-value cap.

    An over-cap opaque value is dropped (never truncated — truncation would silently corrupt an
    opaque token); a debug line records the drop without ever logging the value.
    """
    if not value:
        return
    if len(value) > ENTRY_PARAM_VALUE_MAX_CHARS:
        logger.debug("dropping telegram inbound param %r: value over the %d-char cap", key, ENTRY_PARAM_VALUE_MAX_CHARS)
        return
    params[key] = value


def _reply_params(option: StoredOption) -> dict[str, str] | None:
    """The opaque entry-params a bridged tap carries from the tapped option.

    The author-set ``reply_id`` and a sectioned-row's ``reply_description`` (each bounded, absent
    when the option carried none). ``None`` when the option carries neither — a
    select/suggested-reply ask's minted options, say.
    """
    params: dict[str, str] = {}
    _put_param(params, "reply_id", option.id)
    _put_param(params, "reply_description", option.description)
    return params or None


def _sanitize_params(params: dict[str, str] | None) -> dict[str, str] | None:
    """``params`` validated against the contract's transport bounds, or ``None`` when empty or violated.

    A violation drops the WHOLE set (which would otherwise 5xx and have Telegram redeliver the same
    poison update forever) and lets the turn proceed without params — the participant's message is
    never lost to a params bound; the refusal names the bound/key, never an opaque value.
    """
    if not params:
        return None
    try:
        validate_entry_params(params)
    except ValueError as exc:
        logger.warning("telegram inbound params rejected (%s); proceeding without params", exc)
        return None
    return params


def _inbound_locale(update: dict[str, object]) -> str | None:
    """The participant's BCP 47 locale off a Telegram update — the sender's ``language_code``.

    The IETF tag Telegram attaches to every ``from``, read off the message or the callback_query.
    Canonicalized defensively: a malformed value is dropped to ``None`` (never a 5xx that would have
    Telegram redeliver the poison update), so the turn still runs, just without a locale hint.
    """
    sender: object = None
    for key in ("message", "callback_query"):
        node = update.get(key)
        if isinstance(node, dict) and isinstance(node.get("from"), dict):
            sender = node["from"]
            break
    if not isinstance(sender, dict):
        return None
    code = sender.get("language_code")
    if not isinstance(code, str) or not code.strip():
        return None
    try:
        return normalize_optional_locale(code)
    except InvalidLocaleError:
        logger.warning("telegram inbound: dropping malformed language_code %r", code)
        return None


def _is_recipient_chat(chat: dict[str, object], settings: TelegramSettings) -> bool:
    """Whether ``chat`` is a configured recipient — matched by numeric id or ``@username``.

    Only these chats may ANSWER an ask question.
    """
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
    params: dict[str, str] | None = None,
) -> Response | None:
    """Resolve a ForceReply answer against its pending ask via the ONE shared ladder.

    The correlation key is the anchor message scoped by its chat (a Telegram
    ``message_id`` is unique only per chat, so ``{chat_id}:{message_id}`` is what keeps
    chat B's reply from resolving chat A's same-id ask); the answer is the reply text
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
    our_identity = _our_identity(settings)
    if isinstance(our_identity, JSONResponse):
        return our_identity

    result = await tai42_app.channels.handle_inbound_answer(
        channel_id="telegram",
        correlation_key=scoped_correlation_key(str(chat_id), str(replied_id)),
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
            # Opaque tap enrichment (reply_id / reply_description); the ladder threads it to
            # the callback door on a forward and onto the bridged turn when the reply digresses.
            params=_sanitize_params(params),
        ),
    )
    if result.outcome is InboundAnswerOutcome.NO_CORRELATION:
        return None
    return JSONResponse({"data": {"status": _ACK_STATUS[result.outcome]}}, status_code=200)


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


async def _bridge(
    settings: TelegramSettings,
    chat_id: int,
    text: str,
    update: dict[str, object],
    params: dict[str, str] | None = None,
) -> Response:
    """Hand an uncorrelated user message to the conversation bridge.

    ``our_identity`` is this bot's numeric id (malformed token -> loud 500);
    ``client_address`` is the numeric chat id; ``provider_message_id`` is the
    update id. ``params`` are the channel's opaque tap enrichment (reply_id /
    reply_description — see the module's vocabulary block), validated against the
    contract's transport bounds and dropped whole on a violation so a poison value never
    5xx-loops. No route bound or blank text -> ack + log; a transient failure propagates
    (500).
    """
    update_id = update.get("update_id")
    if not isinstance(update_id, int):
        return JSONResponse({"error": "update carries no integer update_id"}, status_code=400)
    our_identity = _our_identity(settings)
    if isinstance(our_identity, JSONResponse):
        return our_identity

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
            params=_sanitize_params(params),
            locale=_inbound_locale(update),
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
    """A message that becomes a turn: its chat, numeric chat id, turn text, and media params.

    ``media_params`` is the opaque ``media_*`` vocabulary for a media turn, ``None`` for a
    plain text turn.
    """

    chat: dict[str, object]
    chat_id: int
    text: str
    media_params: dict[str, str] | None


class _Unsupported(NamedTuple):
    """A content this channel recognises but cannot map to a turn — the vendor member word refused."""

    chat_id: int
    kind: str


class _MediaSpec(NamedTuple):
    """How one Telegram media member maps onto the generic inbound-media vocabulary."""

    member: str
    kind: InboundMediaKind
    has_mime: bool
    has_filename: bool
    voice: bool
    animated_field: str | None


# Dict-valued media members, in resolution order: ``animation`` before ``document`` because
# Telegram sends an animation as BOTH members and the animation mapping is the correct one.
_MEDIA_SPECS = (
    _MediaSpec("animation", InboundMediaKind.VIDEO, has_mime=True, has_filename=True, voice=False, animated_field=None),
    _MediaSpec(
        "video_note", InboundMediaKind.VIDEO, has_mime=False, has_filename=False, voice=False, animated_field=None
    ),
    _MediaSpec("video", InboundMediaKind.VIDEO, has_mime=True, has_filename=True, voice=False, animated_field=None),
    _MediaSpec("audio", InboundMediaKind.AUDIO, has_mime=True, has_filename=True, voice=False, animated_field=None),
    _MediaSpec("voice", InboundMediaKind.AUDIO, has_mime=True, has_filename=False, voice=True, animated_field=None),
    _MediaSpec(
        "sticker",
        InboundMediaKind.STICKER,
        has_mime=False,
        has_filename=False,
        voice=False,
        animated_field="is_animated",
    ),
    _MediaSpec(
        "document", InboundMediaKind.DOCUMENT, has_mime=True, has_filename=True, voice=False, animated_field=None
    ),
)

# Content Telegram can send that this channel recognises but does not map to a media kind —
# each routes to the one generic unsupported reply + event, never a silent drop.
_UNSUPPORTED_MEMBERS = ("poll", "dice", "venue", "contact", "location", "game")


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _parse_media(message: dict[str, object]) -> tuple[dict[str, str], str] | None:
    """The ``(media_params, placeholder)`` for a media message, or ``None`` when it carries none.

    ``media_params`` is the opaque ``media_*`` vocabulary the bridged turn carries;
    ``placeholder`` is the non-blank turn text used when the message has no caption. A photo
    resolves to its largest size (Telegram sends sizes in ascending order, so the last entry).
    """
    photo = message.get("photo")
    if isinstance(photo, list) and photo:
        largest = photo[-1]
        if isinstance(largest, dict):
            params = build_inbound_media_params(
                kind=InboundMediaKind.IMAGE,
                media_id=_str_or_none(largest.get("file_id")),
                size=_int_or_none(largest.get("file_size")),
            )
            return params, inbound_media_placeholder(InboundMediaKind.IMAGE)
    for spec in _MEDIA_SPECS:
        member = message.get(spec.member)
        if not isinstance(member, dict):
            continue
        filename = _str_or_none(member.get("file_name")) if spec.has_filename else None
        params = build_inbound_media_params(
            kind=spec.kind,
            media_id=_str_or_none(member.get("file_id")),
            mime_type=_str_or_none(member.get("mime_type")) if spec.has_mime else None,
            filename=filename,
            voice=spec.voice,
            animated=bool(member.get(spec.animated_field)) if spec.animated_field else False,
            size=_int_or_none(member.get("file_size")),
        )
        return params, inbound_media_placeholder(spec.kind, filename=filename, voice=spec.voice)
    return None


def _message_fields(message: dict[str, object]) -> _Bridgeable | _Unsupported | Response:
    """Classify a message into a bridgeable turn, an unsupported-content refusal, or an ack.

    A text message bridges its ``text``; a media message (photo/document/audio/voice/video/
    video_note/animation/sticker) bridges ONE turn carrying the opaque ``media_*`` vocabulary,
    with the caption as the turn text or a non-blank placeholder when there is none. A content
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
        return _Bridgeable(chat, chat_id, text, None)
    media = _parse_media(message)
    if media is not None:
        media_params, placeholder = media
        caption = message.get("caption")
        caption = caption.strip() if isinstance(caption, str) and caption.strip() else None
        return _Bridgeable(chat, chat_id, caption or placeholder, media_params)
    for member in _UNSUPPORTED_MEMBERS:
        if member in message:
            return _Unsupported(chat_id, member)
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
    chat, chat_id, text, media_params = fields

    # Signal "working on it" the moment a processable message lands — a typing
    # action shown BEFORE the ask/bridge split so it covers both paths. A delivery
    # failure is logged, never raised: it must not fail the webhook (Telegram
    # would redeliver the whole update).
    try:
        await send_chat_action(chat_id, "typing")
    except ChannelDeliveryError as exc:
        logger.warning("telegram inbound: typing action for chat_id=%s failed: %s", chat_id, exc)

    # ask wins when a ForceReply reply from a recipient chat matches a
    # still-pending question; a correlation miss (expired/never-ours) falls through to
    # the bridge — the shared ladder returns NO_CORRELATION and the caller bridges.
    reply_to = message.get("reply_to_message")
    replied_id = reply_to.get("message_id") if isinstance(reply_to, dict) else None
    if isinstance(replied_id, int) and _is_recipient_chat(chat, settings):
        resolved = await _resolve_answer(settings, replied_id, chat_id, text, update, params=media_params)
        if resolved is not None:
            return resolved

    return await _bridge(settings, chat_id, text, update, params=media_params)
