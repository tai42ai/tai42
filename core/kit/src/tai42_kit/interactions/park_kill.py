"""The whole-chain kill teardown seam: each driver tears its own run state down when the platform kills a park.

A whole-chain kill tears a parked (or running) run down for good: the platform prunes its park
index and clears its continuation-due record, and each driver that owns durable run state tears
its OWN state down (its checkpoint, its resolution records, and every run it linked above it).
Drivers register a handler here at import; the platform fires every handler from its kill path
with the run-authorization context bound around the fire (so a handler's cross-driver teardown
notify authorizes on the chain lineage the killed interaction recorded), and the platform — never
a driver — delivers the run's single FAILED afterward.

A handler is fired with ``(interaction_id, reason)`` and returns ``None``. It owns ONLY its
driver's teardown: a handler that does not own the interaction is a no-op. A handler that cannot
finish yet (its own park record is not written, a link is still in flight, or a cross-driver
teardown notify failed) RAISES; the raise propagates so the platform keeps the durable kill-due
record and the reaper redelivers the kill idempotently. What stays best-effort inside a driver's
abort layer is only the LOCAL finalize of already-dead local state.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeAlias

__all__ = ["ParkKillHandler", "fire_park_killed", "register_park_kill_handler"]

ParkKillHandler: TypeAlias = Callable[[str, str], Awaitable[None]]  # noqa: UP040

_park_kill_handlers: list[ParkKillHandler] = []


def register_park_kill_handler(handler: ParkKillHandler) -> None:
    """Register a driver teardown fired when a parked/running run is killed whole-chain.

    Every registered handler is fired (in registration order) with the killed
    ``(interaction_id, reason)``; a handler that does not own the interaction is a no-op. A driver
    registers once at import. Handlers accumulate — each driver that resumes parks registers its
    own — so the platform reaches every driver's teardown from one fire.
    """
    _park_kill_handlers.append(handler)


async def fire_park_killed(interaction_id: str, reason: str) -> None:
    """Fire every registered driver teardown for a killed run, in registration order.

    Called by the platform's kill path with the run-authorization context already bound. A handler
    that RAISES propagates immediately (a later handler is not fired this pass); the caller keeps
    the durable kill-due record and the reaper redelivers, re-firing every handler — so each
    driver's teardown must be idempotent. With no handler registered this is a no-op (a host with
    no resuming driver has nothing to tear down).
    """
    for handler in _park_kill_handlers:
        await handler(interaction_id, reason)
