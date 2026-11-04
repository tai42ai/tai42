"""Deferring the fleet recycle decision of a caller's changes until the caller has finished its own work.

A caller whose own work must be finished before any worker restarts opens a
:func:`deferring_rolls` scope around its changes. Inside it, every pipeline run persists, reloads
locally, broadcasts and pins the census as always, then records its reload in the scope's
:class:`DeferredRolls` (:class:`DeferredReload`: the writer, the pin, the reload report) instead
of deciding and rolling; its :class:`~tai42_skeleton.config.service.ApplyResult` reads
``roll_deferred`` and carries no verdict yet. A run whose propagation raised records its reload
too, so the scope's decision reads the latest statement of the fleet. Once the caller's work is
done it decides and rolls ONCE over the scope
(:meth:`~tai42_skeleton.config.service.ConfigService.run_deferred_rolls`) and folds the
:class:`~tai42_skeleton.config.recycle_roll.RollOutcome` into its result (:meth:`ApplyResult.rolled
<tai42_skeleton.config.service.ApplyResult.rolled>`). Outside a scope every run decides inline.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind


@dataclass(frozen=True)
class DeferredReload:
    """One run's reload awaiting its recycle decision.

    The writer it was written for, the recycle targets pinned from the census before its local
    reload, and the report its recycle decision reads: its reload report, with the writer's own
    row kept when the broadcast could not reach the bus and the writer's entry answers for its env
    (:func:`~tai42_skeleton.config.service.broadcast._decision_report`).
    """

    writer: WorkerIdentity
    pinned: Mapping[str, tuple[WorkerKind, int]]
    report: FleetResult


@dataclass
class DeferredRolls:
    """The reloads one scope's runs recorded, in the order they ran."""

    reloads: list[DeferredReload] = field(default_factory=list)

    def merged(self) -> DeferredReload | None:
        """The scope's reloads as the one the decision reads, ``None`` when no run recorded one.

        Its pin is every target any run pinned, with the most recent pin of each (the latest
        life the census showed); its report and writer are the latest run's: the scope's latest
        reload is the fleet's latest statement of the recycle-class keys each worker still has
        to respawn for, and every run in a scope is written by the same process.
        """
        if not self.reloads:
            return None
        pinned: dict[str, tuple[WorkerKind, int]] = {}
        for reload in self.reloads:
            pinned.update(reload.pinned)
        latest = self.reloads[-1]
        return DeferredReload(writer=latest.writer, pinned=pinned, report=latest.report)


_ACTIVE: ContextVar[DeferredRolls | None] = ContextVar("deferred_rolls", default=None)


@contextmanager
def deferring_rolls() -> Iterator[DeferredRolls]:
    """Defer the recycle decision of every pipeline run inside the block, recording its reload in the returned scope."""
    deferral = DeferredRolls()
    token = _ACTIVE.set(deferral)
    try:
        yield deferral
    finally:
        _ACTIVE.reset(token)


def active_deferral() -> DeferredRolls | None:
    """The scope the current task's pipeline runs record their reloads in, ``None`` outside one."""
    return _ACTIVE.get()
