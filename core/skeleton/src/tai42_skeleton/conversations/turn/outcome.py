"""The internal turn-outcome value types and their constructors.

A turn resolves to exactly one of a silent no-reply or a produced (answered/error)
outcome; these are the two shapes plus the helpers that build an answer part, the
client-safe error text, and the serialized form of an agent's structured final.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from tai42_contract.conversations import AnswerPart, ConversationRoute, joined_answer_text

# Client-safe text for a failed turn; the internal detail goes to the record's ``error``.
_ERROR_ANSWER_TEXT = "Sorry, something went wrong handling your message. Please try again."


def _serialize_structured(data: object) -> str:
    if isinstance(data, str):
        return data
    model_dump_json = getattr(data, "model_dump_json", None)
    if callable(model_dump_json):
        return str(model_dump_json())
    return json.dumps(data, default=str)


#: Internal sentinel: the agent turn parked on an async ``ask`` instead of answering.
#: Its resumed answer is delivered out of band by the completion continuation, so the turn
#: produces no reply now.
class _AgentParked:
    __slots__ = ()


_AGENT_PARKED = _AgentParked()


@dataclass(frozen=True)
class _SilentOutcome:
    """A tool turn that produced no reply — a designed no-reply, never an error.

    On the channel door nothing is ever sent (terminal ``silent``); on the api door an explicit
    silent marker is delivered through the durable machine.

    ``note`` is an OPTIONAL internal detail (recorded, never delivered) that names WHY the
    turn is silent when that is worth keeping — set for a turn that went silent because the
    run PAUSED with its reply still pending, so the record reads as not-yet-answered rather
    than a plain designed no-reply. ``None`` for an ordinary silent outcome, which records no
    detail (byte-identical to before).
    """

    note: str | None = None


@dataclass(frozen=True)
class _ResolvedOutcome:
    """A turn that produced an outcome to deliver: an ``answered`` reply or a client-safe ``error``.

    ``parts`` is the ORDERED, non-empty list of rich :class:`AnswerPart` messages the turn
    produced — one for a single-message answer, several for an ordered multi-message one (a tool
    route emitting an array of strings and/or part objects). ``answer`` is the part MESSAGE texts
    joined with a blank line — the whole-text form every legacy reader keeps consuming.
    """

    answer_status: Literal["answered", "error"]
    parts: list[AnswerPart]
    error: str | None

    @property
    def answer(self) -> str:
        """The part messages as one joined string.

        What intake dedup, transcripts and the api door body read, and byte-identical to the old
        single ``answer`` for one part. A media-only part contributes nothing, so an all-media
        outcome joins to ``""``.
        """
        return joined_answer_text(self.parts)


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
_ToolOutcome = _SilentOutcome | _ResolvedOutcome | _SupersededOutcome


def _text_part(text: str) -> AnswerPart:
    """A plain text-only :class:`AnswerPart`.

    The shape the platform's own replies (agent answers, tool string replies, greetings,
    error/slow-down text, pairing replies) take.
    """
    return AnswerPart(message=text)


def _error_answer_text(route: ConversationRoute | None) -> str:
    """The participant-facing text for a failed turn.

    The route's configured ``error_reply_text`` when it carries one, else the built-in English
    default. A ``None`` route (no route in scope) falls back to the default. Only the
    participant-facing ``answer`` resolves through the route — the record's ``error`` detail and
    the logs keep the built-in wording.
    """
    return (route.error_reply_text if route is not None else None) or _ERROR_ANSWER_TEXT


def _tool_error(detail: str, route: ConversationRoute | None = None) -> _ResolvedOutcome:
    return _ResolvedOutcome(answer_status="error", parts=[_text_part(_error_answer_text(route))], error=detail)


def _pairing_reply(text: str) -> _ResolvedOutcome:
    return _ResolvedOutcome(answer_status="answered", parts=[_text_part(text)], error=None)
