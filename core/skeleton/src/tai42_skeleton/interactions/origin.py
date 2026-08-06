"""The run/thread origin an ``ask_user`` question is attributed to.

A background tool run binds its run id here for the length of the run, so a
question raised inside the run's tool body carries that origin on its durable
record. Unset (``None``) outside a bound tool run — a direct/sync/agent ask has
no run-scoped identity to stamp. Copied into the offload thread by
``asyncio.to_thread`` like any contextvar, so a sync tool body still reads it.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

__all__ = [
    "get_interaction_origin",
    "reset_interaction_origin",
    "set_interaction_origin",
]

_current_origin: ContextVar[str | None] = ContextVar("tai42_interaction_origin", default=None)


def get_interaction_origin() -> str | None:
    """The origin bound to the current run, or ``None`` outside a bound tool run."""
    return _current_origin.get()


def set_interaction_origin(origin: str | None) -> Token[str | None]:
    """Bind ``origin`` as the current run's interaction origin; pass the returned
    token to :func:`reset_interaction_origin` to restore the previous value."""
    return _current_origin.set(origin)


def reset_interaction_origin(token: Token[str | None]) -> None:
    """Restore the interaction origin to the value captured in ``token``."""
    _current_origin.reset(token)
