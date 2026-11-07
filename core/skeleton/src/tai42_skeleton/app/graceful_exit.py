"""A worker's departure: the writer's own graceful exit, composed over the bus's one exit latch.

A worker leaves through its one exit latch: a decision to leave, taken once, which begins the
life's leaving — the latch and the ``recycling`` census row flip together on the bus — then the
single SIGTERM a process life sends itself (:meth:`~tai42_skeleton.app.bus.WorkerBus.request_graceful_exit`
decides and signals at once; :meth:`~tai42_skeleton.app.bus.WorkerBus.decide_departure` decides
only when no in-flight change holds the exit, and the signal follows the departure's announce).
Its already-installed graceful handler runs, so
in-flight work finishes before the process ends, within that handler's own bound. For a serve
worker that is uvicorn's shutdown, which stops accepting and lets in-flight requests send their
responses; the worker ends its event streams, the ones MCP replies ride, only once those replies
are written and its in-process tool runs have ended (:mod:`~tai42_skeleton.app.serve_exit`), and
uvicorn's ``timeout_graceful_shutdown`` caps that stop. For a backend worker it is the backend's
drain, which lets the in-flight job finish and record its result. The latch acts once: a door that finds the
worker already leaving sends nothing and says so, and a change that reaches the worker after the
decision is refused before it begins (:mod:`~tai42_skeleton.app.writer_exit`).

Every door that decides a worker must leave ends at that latch. Inside the worker's process:
the recycle handler decides to leave and arms the signal on the bus's post-reply slot (fired
once the terminal reply was sent), or, when the worker's own in-flight changes hold its exit,
leaves once they are all answered (:func:`leave_when_settled`); the
join and the withdraw refusal call it, the lifespan calls it when a perpetual background task
dies, and the writer of a change departs through :func:`depart` once its reply is answered
(:mod:`~tai42_skeleton.app.writer_exit`).
From outside the worker's process, a roll reaches the latch as the bus ``Recycle`` op, and a stop
(a deploy, ``docker stop``, a scale-down) as its first stop signal, which the process's host
observes (:meth:`~tai42_skeleton.app.bus.WorkerBus.stop_began`) and sends nothing more for.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tai42_skeleton.app.bus import ExitHold, WorkerBus

logger = logging.getLogger(__name__)


async def depart(bus: WorkerBus, reason: str, holds: Sequence[ExitHold] = ()) -> bool:
    """The writer's own graceful exit, for ``reason``: ``True`` when this call started it.

    ``holds`` are the deferred exits (:meth:`~tai42_skeleton.app.bus.WorkerBus.defer_exit`) the
    writer's change holds on its own member bus; they are released first, on every path, so this
    exit never leaves one held. The exit then waits until no other in-flight change holds it
    (each answers its reply first, and holds the exit no longer), so one exit follows them all
    (:func:`leave_when_settled`). A change that owes this exit never runs under a bound: a
    bounded change hands its whole roll, the writer's exit included, to the member it acts for
    (:func:`~tai42_skeleton.config.recycle_roll.decide_and_roll`), so this wait is never cut. A
    forked job child owns no exit and raises: each of its changes runs in a dispatched job, which
    is bounded, so it never owes one.
    """
    for hold in holds:
        bus.release_deferred_exit(hold)
    return await leave_when_settled(bus, reason)


async def leave_when_settled(bus: WorkerBus, reason: str) -> bool:
    """This member's graceful exit for ``reason`` once no in-flight change holds it; ``True`` when this call started it.

    Waits until no change this worker runs holds its exit
    (:meth:`~tai42_skeleton.app.bus.WorkerBus.deferred_exits_settled`), then decides to leave in
    one step with that check (:meth:`~tai42_skeleton.app.bus.WorkerBus.decide_departure`): a
    change that held the exit meanwhile is waited for in turn, and once the decision is taken a
    carried roll or any other change refuses to start. A worker already leaving by then is logged
    at INFO and left alone. Otherwise it waits for the decision's ``recycling`` announce
    (:meth:`~tai42_skeleton.app.bus.WorkerBus.announce_departure`), so the census reads the
    departing life as a gap and waits for its successor, then sends the departure's signal. The
    decision is final: an announce that fails still sends the signal, then raises.
    """
    while True:
        await bus.deferred_exits_settled()
        refusal = bus.decide_departure(reason)
        if refusal is None:
            break
        if refusal.kind == "already_leaving":
            logger.info(
                "graceful exit: this worker is already leaving (%s) — not asked again for %s", refusal.reason, reason
            )
            return False
    await bus.announce_departure()
    bus.signal_departure()
    return True
