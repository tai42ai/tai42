"""The writer's own exit a change holds and owes, carried from the change to the edge that answers it.

A change this worker runs holds the worker's exit from the change's start
(:func:`hold_writer_exit`, called by every writing door as its change begins: the config
service's write exclusion, the fleet soft restart and the join), so a recycle op from another
worker's roll that reaches the worker mid-change leaves it to that change instead of ending it
mid-reply. A change whose roll converged with the writer needing its own restart owes the
writer's exit after the change's reply. The roll
(:func:`~tai42_skeleton.config.recycle_roll.decide_and_roll`, the one place every reload's roll
flows through) reuses the change's hold and records that debt in the open
:class:`WriterExitScope`. The edge that opened the scope answers the change and then settles it
once:

* the HTTP edge (:class:`~tai42_skeleton.middleware.writer_exit.WriterExitMiddleware`) for every
  route, once the reply's last body chunk is sent;
* the projected tool edge (:mod:`~tai42_skeleton.operations.projection`) for every tool door — MCP,
  ``tai tools run``, an agent, a preset, a hook, a schedule or a submit — once the call returned
  or raised, before the call answers: the exit is the worker's graceful stop, which lets the
  in-flight call return and its reply ship;
* the join roll (:mod:`~tai42_skeleton.app.lifecycle.bus_subscription`), once its roll ended;
* the roll a bounded change handed to its member (:mod:`~tai42_skeleton.config.roll_handoff`),
  once that roll ended.

A change run under a bound (a dispatched job's budget, an armed turn budget) owes no exit to its
edge: the exit waits for every other change holding the worker's exit, which the bound cannot
hold, so the change hands its whole roll, the writer's exit included, to its member, whose
carried roll settles it with no bound over it.

Settling a debt departs (:func:`~tai42_skeleton.app.graceful_exit.depart`), which owns the held
exits from then on; settling a scope that owes nothing releases its hold, and a recycle op that
reached the worker while it was held is carried out then. A scope that closes without its debt
settled — its reply was never sent, or its edge raised or was cancelled before settling —
releases the held exits and logs the debt at ERROR, so the writer is again a worker awaiting a
respawn, never one whose exit is held for good. A roll that owes the exit with no scope open
raises: no edge would ever run it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_skeleton.app.bus import ExitHold, WorkerBus

logger = logging.getLogger(__name__)

WRITER_EXIT_REASON = "the writer's own restart after its change's roll"
"""The reason the writer of a change leaves for, as a door that finds it already leaving reads it."""


class WriterExitScope:
    """The writer's exit the changes run under one edge hold and owe, settled once by that edge.

    Open from the edge's start until it settles or closes. A change run under it holds the
    worker's exit from the change's start (:meth:`hold_exit`), so a recycle op from another
    worker's roll leaves the worker to it until the edge answered; a roll records the exit its
    change owes into it (:meth:`owe`) only while it is open.
    """

    def __init__(self, enclosing: WriterExitScope | None = None) -> None:
        """Open, holding and owing nothing, inside ``enclosing`` (the scope open where this one opened, if any)."""
        self._enclosing = enclosing
        self._bus: WorkerBus | None = None
        self._owes = False
        self._holds: list[ExitHold] = []
        self._open = True

    @property
    def is_open(self) -> bool:
        """Whether a change can still hold or owe the writer's exit in this scope (it is neither settled nor closed)."""
        return self._open

    def hold_exit(self, bus: WorkerBus, change: str) -> None:
        """Hold the worker's exit on ``bus`` for ``change`` until this scope settles or closes.

        One hold serves every change under the scope: a later change, or the roll of one, reuses
        it. A process that does not own its exit (a forked job child) holds none. Raises once the
        scope is closed: the edge already answered, so no one would release the hold.
        """
        if not self._open:
            raise RuntimeError(f"writer exit: {change} began after its edge already answered")
        if self._holds or not bus.owns_exit:
            return
        self._holds.append(bus.defer_exit(change))
        self._bus = bus

    def adopt_hold(self, bus: WorkerBus, hold: ExitHold) -> None:
        """Hold the worker's exit on ``bus`` through ``hold``, taken already, until this scope settles or closes.

        The scope owns ``hold`` from here and releases it on every path. Raises once the scope is
        closed, or when it holds an exit already: one hold serves every change under a scope.
        """
        if not self._open:
            raise RuntimeError(f"writer exit: {hold.reason} handed its hold over after its edge already answered")
        if self._holds:
            raise RuntimeError(f"writer exit: {hold.reason} handed its hold to a scope that holds the exit already")
        self._holds.append(hold)
        self._bus = bus

    def owe(self, bus: WorkerBus) -> None:
        """Record that a change owes the writer's exit on ``bus``, run once this scope settles.

        Raises once the scope is closed: no edge would run the exit any more.
        """
        if not self._open:
            raise RuntimeError("writer exit: a change owes the writer's exit after its edge already answered")
        self._bus = bus
        self._owes = True

    def _take(self) -> tuple[WorkerBus | None, bool, list[ExitHold]]:
        """Close the scope and hand over what it holds and owes: the bus, the debt and the held exits."""
        self._open = False
        taken = self._bus, self._owes, self._holds
        self._bus, self._owes, self._holds = None, False, []
        return taken

    def _holding_enclosing(self) -> WriterExitScope | None:
        """The nearest open enclosing scope that holds or owes the writer's exit, ``None`` when there is none."""
        enclosing = self._enclosing
        while enclosing is not None:
            if enclosing.is_open and (enclosing._owes or enclosing._holds):
                return enclosing
            enclosing = enclosing._enclosing
        return None

    async def settle(self, reason: str = WRITER_EXIT_REASON) -> None:
        """Close the scope: run the writer's exit it owes, for ``reason``, else release the exit it holds.

        The exit owns the held exits from here (:func:`~tai42_skeleton.app.graceful_exit.depart`)
        and releases them on every path; a failed exit raises. A scope that owes nothing releases
        its hold: a recycle op that reached the worker meanwhile leaves it now
        (:meth:`~tai42_skeleton.app.lifecycle.bus_subscription.BusSubscriptionMixin._apply_recycle`).
        """
        from tai42_skeleton.app.graceful_exit import depart

        bus, owes, holds = self._take()
        if bus is None:
            return
        if not owes:
            for hold in holds:
                bus.release_deferred_exit(hold)
            return
        holder = self._holding_enclosing()
        if holder is not None:
            # An enclosing edge holds the exit until its own answer: this exit waiting on that
            # hold would never end, so the enclosing scope owes it instead and the one exit
            # follows the enclosing answer.
            holder._bus = bus
            holder._owes = True
            holder._holds.extend(holds)
            return
        await depart(bus, reason, holds)

    def abandon(self, why: str) -> None:
        """Close the scope without the exit it owes: release the held exits, ERROR-logging ``why`` when it owed one.

        A no-op when it holds and owes nothing.
        """
        bus, owes, holds = self._take()
        if bus is None:
            return
        for hold in holds:
            bus.release_deferred_exit(hold)
        if owes:
            logger.error(
                "writer exit: a change rolled the fleet and owes this writer's own restart, but %s — the exit "
                "is not run and this worker awaits a respawn",
                why,
            )


_scope: ContextVar[WriterExitScope | None] = ContextVar("tai42_writer_exit_scope", default=None)


@contextmanager
def writer_exit_scope() -> Iterator[WriterExitScope]:
    """Open a writer-exit scope for the edge's work; the changes under it owe the writer's exit into it.

    The edge settles it (:meth:`WriterExitScope.settle`) once it answered; on leaving the block a
    debt left unsettled is abandoned (:meth:`WriterExitScope.abandon`), ERROR-logged and released.
    A scope opened inside another one (a projected tool an HTTP route runs) owns the changes under
    it and settles them itself, unless an open enclosing scope holds or owes the exit when an exit
    is owed here: then the enclosing scope takes it over and the one exit follows the enclosing answer.
    """
    scope = WriterExitScope(_scope.get())
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)
        scope.abandon("its edge closed before the exit ran (the reply was not sent, or the edge failed)")


def open_writer_exit_scope(change: str) -> WriterExitScope:
    """The open writer-exit scope a roll for ``change`` owes the writer's exit into.

    Raises when none is open: the roll is reached from a door no edge settles, so the exit it
    owes would never run.
    """
    scope = _scope.get()
    if scope is None or not scope.is_open:
        raise RuntimeError(
            f"writer exit: {change} rolls the fleet and owes the writer's own restart, but no writer-exit "
            "scope is open — the door it runs on settles no exit"
        )
    return scope


def hold_writer_exit(bus: WorkerBus, change: str) -> None:
    """Hold the worker's exit for ``change`` in the open writer-exit scope, from the change's start to its answer.

    Every door's change calls this when it begins (:meth:`WriterExitScope.hold_exit`); the roll
    that settles it reuses the hold. With no scope open the door settles no exit and nothing is
    held: a roll of that change that would owe the writer's exit raises
    (:func:`open_writer_exit_scope`).
    """
    scope = _scope.get()
    if scope is not None and scope.is_open:
        scope.hold_exit(bus, change)
