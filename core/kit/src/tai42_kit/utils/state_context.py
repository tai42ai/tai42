"""The ambient state-context carrier — the one contextvar every door deposits a
:class:`~tai42_contract.states.StateContext` on so downstream state writes resolve
their subject and complete their write provenance without a per-door argument.

Homed in kit so both the skeleton that reads it and the execution backends below the
skeleton (which never import tai42-skeleton) can deposit it, the same layering reason
:mod:`tai42_kit.utils.detached_util` lives here: a backend worker firing a scheduled
job wraps the tool run in :func:`state_context` before the skeleton's write chokepoint
reads it back through :func:`current_state_context`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from tai42_contract.states import StateContext

_current_state_context: ContextVar[StateContext | None] = ContextVar("tai42_states_context", default=None)


@contextmanager
def state_context(ctx: StateContext) -> Iterator[None]:
    """Deposit ``ctx`` as the ambient state context for the duration of the block — a
    door wraps the work it drives so every downstream write completes its provenance
    from it, and the token is reset in the ``finally`` so it never leaks past the door."""
    token = _current_state_context.set(ctx)
    try:
        yield
    finally:
        _current_state_context.reset(token)


def current_state_context() -> StateContext | None:
    """The ambient :class:`StateContext` the current door deposited, or ``None`` outside
    a door."""
    return _current_state_context.get()
