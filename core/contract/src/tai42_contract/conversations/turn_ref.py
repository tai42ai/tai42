"""The ambient in-process conversation-turn seam.

For the duration of a conversation turn the platform deposits WHICH turn is in flight here;
in-process code reads it to learn the turn it serves without the reference being threaded
through every call. Outside any turn the reader returns ``None``.

The channel lives in the CONTRACT (not the skeleton) on purpose: a reader (a tool body, an
engine node) and the turn engine that arms it can sit in different packages that share only
:mod:`tai42_contract`, so the ambient channel must live in the layer both may import. The
contract interprets NOTHING about the deposited reference — a logic-free channel, mirroring
the tool-invocation ContextVar discipline.

A task created inside a deposited block runs on a COPY and keeps the deposit for its lifetime,
so the engine's fan-out and a detached continuation stay attributed to the turn.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from pydantic import BaseModel, ConfigDict


class ConversationTurnRef(BaseModel):
    """The conversation turn currently in flight.

    ``thread_id`` is the turn's canonical thread, ``message_id`` the lead message the turn runs
    on, ``route_name`` the route it runs under. Frozen — a deposited reference is a fact of the
    active turn, never mutated in place.
    """

    model_config = ConfigDict(frozen=True)

    thread_id: str
    message_id: str
    route_name: str


_current_conversation_turn: ContextVar[ConversationTurnRef | None] = ContextVar(
    "tai42_current_conversation_turn", default=None
)


def current_conversation_turn() -> ConversationTurnRef | None:
    """The conversation turn in flight for the current context, or ``None`` outside any turn."""
    return _current_conversation_turn.get()


def set_conversation_turn(turn: ConversationTurnRef) -> Token[ConversationTurnRef | None]:
    """Deposit ``turn`` as the in-flight conversation turn for the current context.

    Pass the returned token to :func:`reset_conversation_turn` to restore the previous value.
    Nested deposits re-set for the inner turn and restore the outer value on reset — ContextVar
    token discipline.
    """
    return _current_conversation_turn.set(turn)


def reset_conversation_turn(token: Token[ConversationTurnRef | None]) -> None:
    """Restore the in-flight conversation turn to the value captured in ``token``.

    ``token`` is the return value of the matching :func:`set_conversation_turn` call.
    """
    _current_conversation_turn.reset(token)


__all__ = [
    "ConversationTurnRef",
    "current_conversation_turn",
    "reset_conversation_turn",
    "set_conversation_turn",
]
