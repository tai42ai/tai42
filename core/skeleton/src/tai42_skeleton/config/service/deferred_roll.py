"""Deferring the fleet recycle rolls of a caller's changes until the caller has finished its own work.

A caller whose own work must be finished before any worker restarts opens a
:func:`deferring_rolls` scope around its changes. Inside it, every pipeline run that owes a roll
persists, reloads locally, broadcasts and pins its recycle targets as always, then records the
roll in the scope's :class:`DeferredRolls` instead of running it; its
:class:`~tai42_skeleton.config.service.ApplyResult` reads ``roll_deferred`` and carries no
verdict yet. Once the caller's work is done it runs them as ONE roll
(:meth:`~tai42_skeleton.config.service.ConfigService.run_deferred_rolls`) and folds the
:class:`RollOutcome` into its result (:meth:`ApplyResult.rolled
<tai42_skeleton.config.service.ApplyResult.rolled>`). Outside a scope every run rolls inline.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_contract.app.responses import AwaitingWorker

    from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind
    from tai42_skeleton.app.recycle import RecycleReport


@dataclass(frozen=True)
class PendingRoll:
    """One run's roll, not yet run: the writer it excludes and the recycle targets pinned before its reload."""

    identity: WorkerIdentity
    pinned: Mapping[str, tuple[WorkerKind, int]]


@dataclass
class DeferredRolls:
    """The rolls one scope's runs deferred, and the last reload report any run in the scope published.

    The verdict of the deferred roll judges ``last_fleet``: the scope's latest reload is the
    fleet's latest statement of the recycle-class keys each worker still has to respawn for.
    """

    rolls: list[PendingRoll] = field(default_factory=list)
    last_fleet: FleetResult | None = None

    def merged(self) -> PendingRoll | None:
        """The deferred rolls as one roll, ``None`` when no run deferred one.

        Its targets are every target any run pinned, with the most recent pin of each (the
        latest life the census showed); the writer is the one the latest run recorded — every
        run in a scope is written by the same process.
        """
        if not self.rolls:
            return None
        pinned: dict[str, tuple[WorkerKind, int]] = {}
        for roll in self.rolls:
            pinned.update(roll.pinned)
        return PendingRoll(identity=self.rolls[-1].identity, pinned=pinned)


@dataclass(frozen=True)
class RollOutcome:
    """What a scope's deferred roll did: its report, the writer it excluded, and the operation's verdict."""

    recycle: RecycleReport
    self_identity: WorkerIdentity
    awaiting_respawn: list[AwaitingWorker]


_ACTIVE: ContextVar[DeferredRolls | None] = ContextVar("deferred_rolls", default=None)


@contextmanager
def deferring_rolls() -> Iterator[DeferredRolls]:
    """Defer the rolls of every pipeline run inside the block, recording them in the returned scope."""
    deferral = DeferredRolls()
    token = _ACTIVE.set(deferral)
    try:
        yield deferral
    finally:
        _ACTIVE.reset(token)


def active_deferral() -> DeferredRolls | None:
    """The scope the current task's pipeline runs defer their rolls to, ``None`` outside one."""
    return _ACTIVE.get()
