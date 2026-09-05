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
from tai42_contract.conversations import (
    ENTRY_PARAM_VALUE_MAX_CHARS,
    BlankInboundTextError,
    validate_entry_params,
)
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
# it — a guest message is never lost to a params bound.

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


def _put_param(params: dict[str, str], key: str, value: str | None) -> None:
    """Add ``key`` iff ``value`` is a non-empty string within the contract's per-value cap.
    An over-cap opaque value is dropped (never truncated — truncation would silently corrupt
    an opaque token); a debug line records the drop without ever logging the value."""
    if not value:
        return
    if len(value) > ENTRY_PARAM_VALUE_MAX_CHARS:
        logger.debug("dropping telegram inbound param %r: value over the %d-char cap", key, ENTRY_PARAM_VALUE_MAX_CHARS)
        return
    params[key] = value


def _reply_params(option: StoredOption) -> dict[str, str] | None:
    """The opaque entry-params a bridged tap carries from the tapped option: the author-set
    ``reply_id`` and a sectioned-row's ``reply_description`` (each bounded, absent when the
    option carried none). ``None`` when the option carries neither — a select/suggested-reply
    ask's minted options, say."""
    params: dict[str, str] = {}
    _put_param(params, "reply_id", option.id)
    _put_param(params, "reply_description", option.description)
    return params or None


def _sanitize_params(params: dict[str, str] | None) -> dict[str, str] | None:
    """``params`` validated against the contract's transport bounds, or ``None`` when empty or
    a bound is violated. A violation drops the WHOLE set (which would otherwise 5xx and have
    Telegram redeliver the same poison update forever) and lets the turn proceed without
    params — the guest's message is never lost to a params bound; the refusal names the
    bound/key, never an opaque value."""
    if not params:
        return None
    try:
        validate_entry_params(params)
    except ValueError as exc:
        logger.warning("telegram inbound params rejected (%s); proceeding without params", exc)
        return None
    return params


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
    try:
        our_identity = bot_numeric_id(
            require_secret(settings.bot_token, "the telegram channel", "CHANNEL_TELEGRAM_BOT_TOKEN")
        )
    except ValueError:
        return _misconfigured("CHANNEL_TELEGRAM_BOT_TOKEN")

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
            params=_sanitize_params(params),
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

    # An inline-keyboard button tap arrives as a callback_query, not a message: it
    # maps the tapped option's index back to its text and resolves/bridges it.
    if isinstance(update.get("callback_query"), dict):
        return await _resolve_callback(settings, update)

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
