"""The internal turn-outcome value types and their constructors.

A turn resolves to exactly one of a silent no-reply, an answered reply, a failure, or a
yield to a newer turn; these are the shapes plus the helpers that build an answer part, the
client-safe error text, the line a failed turn delivers, and the serialized form of an agent's
structured final.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from tai42_contract.conversations import AnswerPart, ConversationRoute, ReplyTexts, joined_answer_text
from tai42_contract.interactions import RunTerminalFailed

from tai42_skeleton.interactions.terminal_failure import failed_outcome_detail

# Client-safe text for a failed turn; the internal detail goes to the record's ``error``.
_ERROR_ANSWER_TEXT = "Sorry, something went wrong handling your message. Please try again."


def _serialize_structured(data: object) -> str:
    if isinstance(data, str):
        return data
    model_dump_json = getattr(data, "model_dump_json", None)
    if callable(model_dump_json):
        return str(model_dump_json())
    return json.dumps(data, default=str)


@dataclass(frozen=True)
class _SilentOutcome:
    """A tool turn that produced no reply — a designed no-reply, never an error.

    On the channel door nothing is ever sent (terminal ``silent``); on the api door an explicit
    silent marker is delivered through the durable machine.

    ``note`` is an OPTIONAL internal detail (recorded, never delivered) that names WHY the
    turn is silent when that is worth keeping — set for a turn that went silent because the
    run PAUSED with its reply still pending, so the record reads as not-yet-answered rather
    than a plain designed no-reply. ``None`` for an ordinary silent outcome, which records no
    detail.
    """

    note: str | None = None


@dataclass(frozen=True)
class _ResolvedOutcome:
    """A turn that produced an ``answered`` reply to deliver.

    ``parts`` is the ORDERED, non-empty list of rich :class:`AnswerPart` messages the turn
    produced — one for a single-message answer, several for an ordered multi-message one (a tool
    route emitting an array of strings and/or part objects). ``answer`` is the part MESSAGE texts
    joined with a blank line — the whole-text form every plain-text reader consumes.
    """

    parts: list[AnswerPart]

    @property
    def answer(self) -> str:
        """The part messages as one joined string.

        What intake dedup, transcripts and the api door body read, and equal to the single part's
        text for one part. A media-only part contributes nothing, so an all-media
        outcome joins to ``""``.
        """
        return joined_answer_text(self.parts)


@dataclass(frozen=True, slots=True)
class _FailedOutcome:
    """A turn that ended failed.

    ``error`` is the internal detail kept on the record and in the logs; ``reply`` is the target's
    own participant-facing text (``RunTerminalFailed.reply``), ``None`` when the target gave none.
    ``leading_parts`` are messages delivered ahead of the failure line (an owed first-contact
    greeting). What, if anything, the participant reads is resolved by :func:`failure_line`
    against the route and who sent the turn, never here.
    """

    error: str
    reply: str | None
    leading_parts: tuple[AnswerPart, ...] = ()


@dataclass(frozen=True)
class _SupersededOutcome:
    """A turn that yielded to a newer message — resolve it ``superseded``, no reply, no delivery.

    Produced when a target raises :class:`~tai42_contract.conversations.TurnSupersededError` (a
    tool that read the pending seam and stopped before an irreversible step); the platform's
    cancel watcher takes the same outcome by a different path (``overlap.supersede_lead``).
    ``successor_id`` is the ``message_id`` of the turn that took this one's place.
    """

    successor_id: str


#: A tool turn resolves to exactly one of these shapes — no coercible in-between.
_ToolOutcome = _SilentOutcome | _ResolvedOutcome | _FailedOutcome | _SupersededOutcome


def _text_part(text: str) -> AnswerPart:
    """A plain text-only :class:`AnswerPart`.

    The shape the platform's own replies (agent answers, tool string replies, greetings,
    error/slow-down text, pairing replies) take.
    """
    return AnswerPart(message=text)


def reply_text(route: ConversationRoute | None, member: str, default: str) -> str:
    """The participant-facing text of one of the platform's fixed replies on ``route``.

    The route's ``reply_texts`` member named ``member`` when the route and that member are set,
    else ``default`` (the built-in English). ``member`` must name a
    :class:`~tai42_contract.conversations.ReplyTexts` member: an unknown name raises
    :class:`KeyError`, so a misspelt member is a loud bug, never a silent default.
    """
    if member not in ReplyTexts.model_fields:
        raise KeyError(member)
    texts = route.reply_texts if route is not None else None
    value = getattr(texts, member) if texts is not None else None
    return value if value is not None else default


def _error_answer_text(route: ConversationRoute | None) -> str:
    """The platform's fixed participant-facing text for a failed turn.

    The route's ``reply_texts.error`` when it carries one, else the built-in English default. A
    ``None`` route (no route in scope) falls back to the default. Only the participant-facing
    ``answer`` resolves through the route — the record's ``error`` detail and the logs keep the
    built-in wording.
    """
    return reply_text(route, "error", _ERROR_ANSWER_TEXT)


def failure_line(
    route: ConversationRoute | None, outcome: _FailedOutcome, *, inbound_kind: Literal["message", "event"]
) -> str | None:
    """The text a failed turn delivers to the participant, ``None`` when nothing is delivered.

    The target's own ``reply`` is always delivered: a consumer that speaks on failure is heard
    whoever sent the turn. The platform's fixed sentence (:func:`_error_answer_text`) is owed only
    to a turn the participant sent (``inbound_kind == "message"``: a text, media or form turn); a
    provider or system notice (``"event"``) that fails with no reply is answered with nothing, and
    the failure stays on the record and in the logs.
    """
    if outcome.reply is not None:
        return outcome.reply
    if inbound_kind == "message":
        return _error_answer_text(route)
    return None


def _tool_error(detail: str) -> _FailedOutcome:
    return _FailedOutcome(error=detail, reply=None)


def _terminal_failure(exc: RunTerminalFailed) -> _FailedOutcome:
    """The failed outcome of a turn whose target RAISED a terminal failure.

    It carries the target's ``reply``; the ``error`` detail is the opaque outcome rendered whole by
    :func:`failed_outcome_detail`, never read key by key.
    """
    return _FailedOutcome(error=failed_outcome_detail(exc.outcome), reply=exc.reply)


def _pairing_reply(text: str) -> _ResolvedOutcome:
    return _ResolvedOutcome(parts=[_text_part(text)])
