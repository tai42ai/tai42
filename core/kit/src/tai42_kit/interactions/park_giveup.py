"""The give-up handler seam: the driver that parked a run tells its waiter when the platform abandons the park.

When the platform gives up redelivering an answered park (its continuation-due record outlived
the redelivery horizon), the waiter to tell is the one the parking DRIVER captured for that
park. The platform names the interaction only; each driver that parks registers one handler,
reads its own record of that interaction, runs its own failed-terminal epilogue and returns the
outermost outcome for the platform to deliver to the run's address. A handler returns ``None``
for an interaction that is not its park.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NamedTuple, TypeAlias

__all__ = [
    "ParkGiveUpHandler",
    "ParkGiveUpOutcome",
    "fire_park_giveup",
    "register_park_giveup_handler",
]


class ParkGiveUpOutcome(NamedTuple):
    """The outermost outcome of a run whose abandoned park its driver gave up, for the platform to deliver."""

    result: Any


ParkGiveUpHandler: TypeAlias = Callable[[str, Mapping[str, Any]], Awaitable[ParkGiveUpOutcome | None]]  # noqa: UP040

_park_giveup_handlers: list[ParkGiveUpHandler] = []


def register_park_giveup_handler(handler: ParkGiveUpHandler) -> None:
    """Register a driver's give-up handler; handlers run in registration order. A driver registers once at import."""
    _park_giveup_handlers.append(handler)


async def fire_park_giveup(interaction_id: str, failed_outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
    """Ask each registered handler in turn to give up ``interaction_id``'s park; return the first one's outcome.

    A handler that owns the park returns its outcome and later handlers are not called; ``None``
    when no handler owns it. A handler's raise propagates.
    """
    for handler in _park_giveup_handlers:
        handled = await handler(interaction_id, failed_outcome)
        if handled is not None:
            return handled
    return None
