"""Reply-capable content handlers: text, interactive tap, and template button.

Each handler reads the participant's words from the message first; an unreadable body gets the
one unreadable notice and nothing else. Readable words are routed once: they resolve a pending
question via the shared answer ladder, or go to the conversation bridge.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from tai42_contract.conversations import InboundRejectionReason
from tai42_kit.conversations.entry_params import put_entry_param

from tai42_channel_whatsapp.correlation import PendingQuestion, already_seen, peek_pending
from tai42_channel_whatsapp.inbound.answers import _bridge_inbound, _resolve_answer
from tai42_channel_whatsapp.inbound.forms import _handle_form_reply
from tai42_channel_whatsapp.inbound.params import _merged_params, _MessageContext
from tai42_channel_whatsapp.inbound.rejection import _notify_rejected

# The interactive subtypes this channel reads as a tap; ``nfm_reply`` (a completed form) is read
# by the form handler. Any other subtype is content this channel has no reader for.
_TAP_REPLY_TYPES = ("button_reply", "list_reply")


class _Words(NamedTuple):
    """The participant's readable words: non-blank ``text`` (verbatim) and the entry-params it carries."""

    text: str
    params: dict[str, str]


def _non_blank(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _read_text(message: dict[str, Any]) -> _Words | None:
    """A text message's ``text.body``, or ``None`` when it is missing or blank."""
    text_field = message.get("text")
    body = _non_blank(text_field.get("body") if isinstance(text_field, dict) else None)
    return None if body is None else _Words(body, {})


def _read_button(message: dict[str, Any]) -> _Words | None:
    """A template quick-reply's visible ``button.text`` with its ``button_payload``, or ``None`` when blank."""
    button = message.get("button")
    if not isinstance(button, dict):
        return None
    text = _non_blank(button.get("text"))
    if text is None:
        return None
    params: dict[str, str] = {}
    put_entry_param(params, "button_payload", button.get("payload"), door="whatsapp")
    return _Words(text, params)


def _interactive_subtype(interactive: dict[str, Any]) -> str | None:
    """The interactive message's subtype, or ``None`` when it names none."""
    subtype = interactive.get("type")
    return subtype if isinstance(subtype, str) and subtype else None


def _extract_interactive_reply(interactive: dict[str, Any], subtype: str) -> tuple[str | None, Any, str | None]:
    """The tapped ``(id, title, description)`` of a tap of ``subtype``.

    ``id`` is None when the reply object is missing or malformed; ``title`` is the raw label
    value; ``description`` is a ``list_reply``'s optional secondary line (``None`` for a button
    reply, which has none).
    """
    reply = interactive.get(subtype)
    if not isinstance(reply, dict):
        return None, None, None
    reply_id = reply.get("id")
    description = reply.get("description")
    return (
        (reply_id if isinstance(reply_id, str) else None),
        reply.get("title"),
        (description if isinstance(description, str) else None),
    )


def _read_tap(interactive: dict[str, Any], subtype: str) -> tuple[str | None, _Words] | None:
    """A tap's reply id and its title as the words (carrying ``reply_id``/``reply_description``), or ``None``.

    ``None`` when the title is missing or blank: there is no label to bridge.
    """
    reply_id, raw_title, description = _extract_interactive_reply(interactive, subtype)
    title = _non_blank(raw_title)
    if title is None:
        return None
    params: dict[str, str] = {}
    put_entry_param(params, "reply_id", reply_id, door="whatsapp")
    put_entry_param(params, "reply_description", description, door="whatsapp")
    return reply_id, _Words(title, params)


async def _route_words(
    phone_number_id: str,
    wa_id: str,
    wamid: str,
    words: _Words,
    context: _MessageContext,
    *,
    answer_for: Callable[[PendingQuestion], str | None],
) -> None:
    """Answer the pending question with the words, or bridge them as a turn.

    The pending question is read NON-destructively (``peek_pending``); ``answer_for`` maps it to
    the answer these words give, or ``None`` when they are not an answer to it (a stale or
    malformed tap, a tap against a text ask): the words are then bridged and the ask is left
    untouched. With nothing pending the words are bridged. The bridged turn carries the
    message-level ``context`` params merged with the words' own; both paths carry the quoted
    message's ``wamid``. One-pending is enforced on reserve and the ``wamid`` dedupe guards a
    redelivery, so the peek-then-resolve needs no destructive claim.
    """
    params = _merged_params(context.params, words.params)
    pending = await peek_pending(phone_number_id, wa_id)
    answer = answer_for(pending) if pending is not None else None
    if pending is None or answer is None:
        await _bridge_inbound(
            phone_number_id,
            wa_id,
            words.text,
            wamid,
            params=params,
            reply_to_medium_message_id=context.reply_to_medium_message_id,
        )
        return
    await _resolve_answer(
        phone_number_id,
        wa_id,
        wamid,
        answer,
        pending,
        params=params,
        reply_to_medium_message_id=context.reply_to_medium_message_id,
    )


async def _refuse_unreadable(phone_number_id: str, wa_id: str, wamid: str, kind: str) -> None:
    await _notify_rejected(phone_number_id, wa_id, wamid, kind=kind, reason=InboundRejectionReason.UNREADABLE)


async def _handle_text(
    message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, context: _MessageContext
) -> None:
    """A typed reply: it answers a pending question with its body minus outer whitespace, else is bridged.

    A text with no or a blank body gets the one unreadable notice (kind ``text``), deduped per
    ``wamid``, with or without a question pending (the question stays open).
    """
    if await already_seen(wamid):
        return
    words = _read_text(message)
    if words is None:
        await _refuse_unreadable(phone_number_id, wa_id, wamid, "text")
        return
    await _route_words(phone_number_id, wa_id, wamid, words, context, answer_for=lambda _pending: words.text.strip())


def _map_tap_to_answer(reply_id: str | None, pending: PendingQuestion) -> str | None:
    """The option text a tap answers, or ``None`` when the tap is NOT an answer.

    A tap answers only when its id's interaction part EQUALS the pending ask's and
    its index is in range for that ask's options. A malformed id, an out-of-range
    index, a mismatched interaction part (a stale button from an earlier ask), or a
    pending question with no options (a text ask) all yield ``None`` — the pending
    question stays open and the caller bridges the tap's title instead.
    """
    if reply_id is None or pending.options is None or pending.interaction_id is None:
        return None
    interaction_part, separator, index_part = reply_id.rpartition(":")
    if not separator or interaction_part != pending.interaction_id or not index_part.isdigit():
        return None
    try:
        index = int(index_part)
    except ValueError:
        # ``isdigit()`` is True for Unicode digit characters (e.g. "²") and for
        # absurdly-long digit strings, both of which ``int()`` rejects — a
        # non-answer, never a propagating 5xx that has Meta redeliver forever.
        return None
    if index >= len(pending.options):
        return None
    return pending.options[index]


async def _handle_interactive(
    message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, context: _MessageContext
) -> None:
    """A button tap or list pick: map it to a pending ask's option, else bridge the tap's title.

    A tap whose id matches the pending ask answers it (``options[index]``). A tap with no
    pending question, or one whose id is stale/malformed/out-of-range, is NOT an answer: the
    pending question is left untouched and the tap's title goes to the bridge like any
    unrelated message — carrying the tapped ``reply_id`` (and a ``list_reply``'s
    ``reply_description``) in ``params`` so a consumer sees WHICH option was tapped — never a
    5xx that would have Meta redeliver the poison tap forever. A tap whose title is missing or
    blank gets the one unreadable notice (kind = the subtype) and leaves a pending question
    open. A completed form (``nfm_reply``) goes to the form handler; an interactive subtype
    this channel has no reader for gets the one unsupported-type notice (kind = the subtype,
    or ``interactive`` when the message names none) and leaves a pending question untouched.
    Every notice is deduped per ``wamid``.
    """
    if await already_seen(wamid):
        return
    raw_interactive = message.get("interactive")
    interactive: dict[str, Any] = raw_interactive if isinstance(raw_interactive, dict) else {}
    subtype = _interactive_subtype(interactive)
    if subtype == "nfm_reply":
        await _handle_form_reply(
            interactive,
            phone_number_id,
            wa_id,
            wamid,
            context.params,
            reply_to_medium_message_id=context.reply_to_medium_message_id,
        )
        return
    if subtype not in _TAP_REPLY_TYPES:
        await _notify_rejected(
            phone_number_id,
            wa_id,
            wamid,
            kind=subtype or "interactive",
            reason=InboundRejectionReason.UNSUPPORTED_TYPE,
        )
        return
    tap = _read_tap(interactive, subtype)
    if tap is None:
        await _refuse_unreadable(phone_number_id, wa_id, wamid, subtype)
        return
    reply_id, words = tap
    await _route_words(
        phone_number_id,
        wa_id,
        wamid,
        words,
        context,
        answer_for=lambda pending: _map_tap_to_answer(reply_id, pending),
    )


async def _handle_button(
    message: dict[str, Any], phone_number_id: str, wa_id: str, wamid: str, context: _MessageContext
) -> None:
    """A template quick-reply tap (a ``button`` message): it answers like a typed reply, else is bridged.

    ``button.text`` is the human-visible label (the turn text every consumer sees);
    ``button.payload`` is the developer-defined payload behind it, carried in ``params`` as
    ``button_payload`` on the bridged turn. On the correlated-answer path the visible text minus
    outer whitespace answers the ask. A button with no or a blank text gets the one unreadable
    notice (kind ``button``), deduped per ``wamid``, and leaves a pending question open.
    """
    if await already_seen(wamid):
        return
    words = _read_button(message)
    if words is None:
        await _refuse_unreadable(phone_number_id, wa_id, wamid, "button")
        return
    await _route_words(phone_number_id, wa_id, wamid, words, context, answer_for=lambda _pending: words.text.strip())
