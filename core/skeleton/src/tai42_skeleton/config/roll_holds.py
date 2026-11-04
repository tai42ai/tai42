"""The named holds the bus member carrying a handed-off fleet recycle roll keeps for the roll's whole run.

A change can hold something across its own roll that a second change must still be refused by
while the roll runs, such as a lock. When the change hands its roll off
(:mod:`~tai42_skeleton.config.roll_handoff`), its own process releases what it holds once it
answers, so it names the holds on the :class:`~tai42_skeleton.app.bus.ops.CarryRoll` op. The member
carrying the roll enters every named hold before it answers the op, while the change still holds
its own, and exits them once the roll ended, so what the change held is never free while the roll
runs. A component registers its holds by name (:func:`register_roll_hold`) when its module loads.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager

RollHold = Callable[[], AbstractAsyncContextManager[None]]

_HOLDS: dict[str, RollHold] = {}


def register_roll_hold(name: str, factory: RollHold) -> None:
    """Register ``factory`` as the roll hold named ``name``; a second registration under ``name`` raises."""
    if name in _HOLDS:
        raise ValueError(f"a roll hold named {name!r} is already registered")
    _HOLDS[name] = factory


@asynccontextmanager
async def enter_roll_holds(names: Sequence[str]) -> AsyncIterator[None]:
    """Hold every roll hold ``names`` names for the body: entered in order, exited in reverse.

    A name no component registered raises :class:`LookupError` naming it before any hold is
    entered. A hold whose entry raises exits the holds already entered and propagates its error.
    """
    factories: list[RollHold] = []
    for name in names:
        factory = _HOLDS.get(name)
        if factory is None:
            raise LookupError(f"no roll hold is registered under {name!r}")
        factories.append(factory)
    async with AsyncExitStack() as stack:
        for factory in factories:
            await stack.enter_async_context(factory())
        yield
