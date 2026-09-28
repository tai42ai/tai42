"""The reply-correlation and conversation-bridge seam shared by the text door and the media path.

Both an inbound text message and an inbound media member route the same way: a ForceReply
reply from a configured recipient chat whose ask is still pending resolves that ask through
the ONE shared ladder; every other message bridges as a fresh turn keyed by this bot's numeric
id and the chat id. That routing, the opaque entry-params it carries, the sender-locale hint,
and the fail-closed identity/ack helpers live here so both callers share one implementation.
"""

from __future__ import annotations

import logging

from starlette.responses import JSONResponse, Response
from tai42_contract.app import tai42_app
from tai42_contract.channels import InboundAnswerOutcome, InboundBridge
from tai42_contract.conversations import (
    ENTRY_PARAM_VALUE_MAX_CHARS,
    BlankInboundTextError,
    validate_entry_params,
)
from tai42_contract.interactions import MediaItem
from tai42_contract.locale import InvalidLocaleError, normalize_optional_locale
from tai42_kit.settings import require_secret

from tai42_channel_telegram.correlation import (
    StoredOption,
    scoped_correlation_key,
    telegram_correlation_store,
)
from tai42_channel_telegram.settings import TelegramSettings, bot_numeric_id

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
    fresh turn (the ladder never bridges on a miss; the caller does). An
    :class:`AnswerForwardError` (401/413/5xx / transport fault) propagates
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


async def _bridge(
    settings: TelegramSettings,
    chat_id: int,
    text: str,
    update: dict[str, object],
    params: dict[str, str] | None = None,
    attachments: list[MediaItem] | None = None,
) -> Response:
    """Hand an uncorrelated user message to the conversation bridge.

    ``our_identity`` is this bot's numeric id (malformed token -> loud 500);
    ``client_address`` is the numeric chat id; ``provider_message_id`` is the
    update id. ``params`` are the channel's opaque tap enrichment (reply_id /
    reply_description — see the module's vocabulary block) plus the parity ``media_*``
    vocabulary of a media turn, validated against the contract's transport bounds and
    dropped whole on a violation so a poison value never 5xx-loops. ``attachments`` is the
    typed served media the participant sent, ingested through the platform seam. No route
    bound or blank text -> ack + log; a transient failure propagates (500).
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
            attachments=attachments,
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


async def _resolve_or_bridge(
    settings: TelegramSettings,
    chat: dict[str, object],
    chat_id: int,
    text: str,
    message: dict[str, object],
    update: dict[str, object],
    params: dict[str, str] | None = None,
    attachments: list[MediaItem] | None = None,
) -> Response:
    """Resolve a recipient's reply against a pending ask, or bridge the message as a fresh turn.

    The ONE reply-correlation shared by the text and media paths: a ForceReply reply is recognised
    by ``reply_to_message.message_id`` (the pending ask's anchor, scoped by its chat). When the
    reply comes from a recipient chat and the anchor is still pending, the shared ladder resolves
    the ask with ``text`` + ``params``; a correlation miss (expired/never-ours), a non-reply, or a
    non-recipient chat falls through to the bridge — the ladder returns NO_CORRELATION and the
    caller bridges.

    The ask's answer is text + ``params`` by contract: the answer ladder carries no typed
    attachments, so a correlated media reply resolves the ask with the caption/placeholder text and
    the parity ``media_*`` params only, while ``attachments`` (the typed served media) rides the
    bridge fallthrough alone.
    """
    reply_to = message.get("reply_to_message")
    replied_id = reply_to.get("message_id") if isinstance(reply_to, dict) else None
    if isinstance(replied_id, int) and _is_recipient_chat(chat, settings):
        resolved = await _resolve_answer(settings, replied_id, chat_id, text, update, params=params)
        if resolved is not None:
            return resolved
    return await _bridge(settings, chat_id, text, update, params=params, attachments=attachments)
