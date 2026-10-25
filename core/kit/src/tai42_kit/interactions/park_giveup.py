"""The give-up handler seam: the driver that parked a run tells its waiter when the park ends in a failure.

A park is given up for one of two causes: the platform abandons an answered park (its
continuation-due record outlived the redelivery horizon), or a preset's output stage refuses the
answer before the driver's continuation receives it. The waiter to tell is the one the parking
DRIVER captured for that park. The caller that knows the cause names the interaction and hands
the typed :class:`ParkGiveUpFailure`; each driver that parks registers one handler, reads its own
record of that interaction, runs its own failed-terminal epilogue with that failure and returns
the outermost outcome for the platform to deliver to the run's address. A handler returns
``None`` for an interaction that is not its park.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import TYPE_CHECKING, Any, NamedTuple, TypeAlias

if TYPE_CHECKING:
    from tai42_contract.errors import ErrorKind

__all__ = [
    "ParkGiveUpFailure",
    "ParkGiveUpHandler",
    "ParkGiveUpOutcome",
    "fire_park_giveup",
    "register_park_giveup_handler",
]


class ParkGiveUpOutcome(NamedTuple):
    """The outermost outcome of a run whose park its driver gave up, for the platform to deliver.

    The park was given up because the platform abandoned it past the redelivery horizon, or
    because a preset's output stage refused its answer.
    """

    result: Any


class ParkGiveUpFailure(NamedTuple):
    """The failure a park is given up with, declared whole by the caller that knows its cause.

    ``outcome`` is the FAILED outcome mapping (opaque; no handler reads a key out of it),
    ``message`` the human text, ``error_type`` the failure's type name, ``kind`` its
    :class:`~tai42_contract.errors.ErrorKind`, and ``transport`` whether it is a failure of a
    transport the platform owns (the contract's transport axis, independent of the kind).
    """

    outcome: Mapping[str, Any]
    message: str
    error_type: str
    kind: ErrorKind
    transport: bool


ParkGiveUpHandler: TypeAlias = Callable[[str, ParkGiveUpFailure], Awaitable[ParkGiveUpOutcome | None]]  # noqa: UP040

_park_giveup_handlers: list[ParkGiveUpHandler] = []


def register_park_giveup_handler(handler: ParkGiveUpHandler) -> None:
    """Register a driver's give-up handler; handlers run in registration order. A driver registers once at import."""
    _park_giveup_handlers.append(handler)


async def fire_park_giveup(interaction_id: str, failure: ParkGiveUpFailure) -> ParkGiveUpOutcome | None:
    """Ask each registered handler in turn to give up ``interaction_id``'s park with ``failure``.

    Each handler is handed ``failure`` unchanged. A handler that owns the park returns its
    outcome, which is returned, and later handlers are not called; ``None`` when no handler
    owns it. A handler's raise propagates.
    """
    for handler in _park_giveup_handlers:
        handled = await handler(interaction_id, failure)
        if handled is not None:
            return handled
    return None
