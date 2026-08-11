"""The turn-scoped bridge context an in-process builtin tool reads.

A builtin tool an AGENT calls during a bridge turn runs IN-PROCESS, in the same asyncio
context tree as the turn engine's ``agent.astream(...)`` await — the same propagation the
bound execution identity relies on (a contextvar set around the agent invocation reaches the
tool, and a task spawned inside the turn runs on a copy that keeps it). This carries the
CURRENT conversation's identifiers so a builtin like ``set_conversation_mode`` can act on the
live thread without the agent supplying them, which an agent turn cannot: it receives only
the message text and its thread id.

``None`` (the default) means no bridge turn is in flight, so a builtin that requires a
current conversation refuses loudly rather than acting on nothing.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class BridgeTurnContext:
    """The current bridge turn's conversation identifiers. ``thread_id`` is the memory/mode
    key; ``route_name``/``channel``/``our_identity``/``client_address`` describe the medium
    the turn is on, carried for builtins that need to name it."""

    thread_id: str
    route_name: str
    channel: str | None
    our_identity: str | None
    client_address: str


_current_bridge_turn: ContextVar[BridgeTurnContext | None] = ContextVar("tai42_current_bridge_turn", default=None)


def current_bridge_turn() -> BridgeTurnContext | None:
    """The bridge turn in flight in the current context, or ``None`` outside one."""
    return _current_bridge_turn.get()


@contextlib.contextmanager
def bridge_turn_context(context: BridgeTurnContext) -> Iterator[None]:
    """Bind ``context`` as the current bridge turn for the block's duration, restoring the
    previous value (per-context, so a task spawned inside keeps it for its own lifetime)."""
    token = _current_bridge_turn.set(context)
    try:
        yield
    finally:
        _current_bridge_turn.reset(token)


__all__ = ["BridgeTurnContext", "bridge_turn_context", "current_bridge_turn"]
