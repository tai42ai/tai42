"""Retiring a serving generation once a build swapped a fresh one in.

The retire cancels the old generation's periodic loops, drains its admitted work (served
requests, session calls, backend jobs on the serving loop) and background runs, resets the
settings caches, closes its transports (its FastMCP lifespan and the sub-app lifespans it
adopted), then its retired client pools and its retired loop-bound registries (checkpoint
savers, stores), and arms the generation's certification
(:func:`~tai42_skeleton.app.retired_generations.arm_retired_generation_certification`).

The drain budget bounds the work that can finish on its own. Some work cannot finish before a
holder of the reload gate releases, which may happen only after the retire returns: work that
drives the reload gate, or waits on it, on another lock whose holder may drive the retire (a
recorded lock), or on work outside this process that may drive it (a recorded wait with no lock).
:class:`~tai42_skeleton.app.reload_gate.GateParty` records such work on its generation; it records
a background run only while the run's generation is live, so a run of the retired generation that
starts such a wait after the swap is drained as an ordinary run. Recorded work is not counted
against the budget and is kept out of the drain. The retire looks for such work when it starts,
after each drain of the admitted work (that drain stops at once when admitted work starts waiting
on a recorded lock), and once the drain of the background runs has ended. A wait that starts
during that drain is seen only if it is still on when that drain has ended; a wait that both
starts and ends inside that drain is never seen: when no look has found such work, the generation
closes synchronously under whatever of its work is still running. Once a look has found such work,
the retire leaves the transports, the client pools and the loop-bound registries open and hands
the drain and the close to a background tail, even when that work stops waiting before the retire
returns: the tail, not the synchronous retire, then drains the work a drain left out. The tail
lets the gate turn over, drains the admitted work within the budget, waits for every driver to
answer, and, while work of the generation still waits on a recorded lock, waits for those waits to
end and repeats; it then drains the generation's background runs within the budget, as the
synchronous retire does, and only then closes. Work that starts a recorded wait after the retire
last looks for one (once the synchronous drains have ended, or once the tail has found no waiter)
is closed under as ordinary work is. A failure of the tail is logged at ERROR with its traceback.
A call of an SSE session is admitted work of its own here, counted until its session stream has
written its reply, so closing the transports — which ends the generation's SSE sessions — never
overtakes a driver's answer.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import TYPE_CHECKING, Final

from tai42_kit.clients import drain_epoch
from tai42_kit.fork_gate import fork_gate
from tai42_kit.loop_bound import close_retired_registries
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.retired_generations import arm_retired_generation_certification

if TYPE_CHECKING:
    from tai42_skeleton.app.epoch import Epoch

logger = logging.getLogger(__name__)

# The drains of real work a retire runs in sequence, each bounded by the drain budget.
RETIRE_DRAIN_STEPS: Final = 3


def retire_drain_floor_seconds(budget: float) -> float:
    """The longest a retire may legally spend draining real work, for the drain budget ``budget``.

    The retire drains the generation's admitted work, then its supervisors' terminal
    writes, then its retired client pools' leases, in sequence, each bounded by the one drain
    budget — so a retire whose work all finishes legally may spend three budgets. The
    periodic-loop cancels that precede the drains are bounded as a guard against a wedged loop
    (an overrun is logged as abandoning it) and are not drain.
    """
    return RETIRE_DRAIN_STEPS * budget


# Strong references to the deferred retire tails, so the loop does not GC a still-running
# one; each removes itself on completion.
_deferred_retire_tasks: set[asyncio.Task[None]] = set()


def deferred_retires() -> frozenset[asyncio.Task[None]]:
    """The deferred retire tails still running, each draining and then closing its retired generation."""
    return frozenset(_deferred_retire_tasks)


def _drain_budget(deadline: float | None) -> float:
    # ``deadline`` here is a relative duration in SECONDS (the drain budget), not an
    # absolute time — mirrors the kit's ``drain_epoch(epoch, deadline)`` vocabulary.
    if deadline is not None:
        return deadline
    from tai42_skeleton.app import instance

    return instance.build_app().drain_budgets.budget()


async def retire_epoch(old: Epoch, retired: int, deadline: float | None) -> None:
    """Retire the previous generation, bounded by the drain budget.

    Cancels the generation's periodic loops first (no timer outlives its epoch), drains
    its admitted work and background supervisors, resets the settings, closes its
    transports, then its retired client pools and loop-bound registries
    (:func:`_close_generation`), and — once
    its admitted work is drained and its transports closed — arms its certification:
    the generation's settings are swept for stale-config leaks once its serving surface is
    collected. Each step is independent
    so one failure cannot skip the rest — the fresh epoch already serves new traffic, so
    a retire fault is loud but never fatal.

    A generation with no work on the reload gate (a bus-delivered reload, a self-resync)
    is drained and closed synchronously. A generation whose admitted work or supervisors drive
    or wait on the gate is ``engaged``: a reload driven through a door runs this retire
    INSIDE the request or job that drives it, and an MCP tool call's response travels on this
    generation's session manager, so closing the transports here would sever the reply,
    and work in a recorded wait may hold a lease on the retired client pools for the whole
    wait. The retire hands the generation to the background tail
    (:func:`_deferred_drain_and_close`) once it has seen it engaged — when it starts, after a
    drain of its admitted work, or after its drain of the background runs — even when the
    engaged work stops waiting before the retire returns. Its drain of the background runs runs
    either way and keeps the engaged runs out; the tail drains the admitted work and the runs
    the synchronous drains left out, and closes the transports, the retired
    client pools and the retired loop-bound registries after every driver has answered and
    every recorded wait has ended. A supervisor is never cancelled by a supervisor drain while
    it drives or waits on the gate; one that waited is drained by the tail once its wait has
    ended.
    """
    budget = _drain_budget(deadline)
    await old._cancel_periodic_loops(budget)
    loop = asyncio.get_running_loop()
    drain_ends = loop.time() + budget
    # ``deferred`` records whether the retire has seen the generation engaged. A drain step
    # that sees it engaged leaves the engaged work out (the admitted drain is skipped or
    # stops, the run drain keeps the engaged runs out), so from then on the drain and the
    # close belong to the tail, even when that work stops being engaged later. The generation
    # is read again after every admitted drain with no await before the run drain, so the run
    # drain keeps a run out only once ``deferred`` is set.
    deferred = old.engaged
    drain_unfinished = not deferred
    while drain_unfinished and not deferred:
        # Drain this generation's admitted work SYNCHRONOUSLY before the transport
        # close below, so a real request (an MCP tool call, a sync REST tool run) finishes
        # on the old epoch and is never severed mid-response, and a backend job on the
        # serving loop ends before its loop-bound registries close. A stream that cannot finish on
        # its own — the interactions-inbox SSE the Studio shell holds, an MCP transport's
        # server-to-client stream — EXEMPTS itself from this drain
        # (``mark_current_request_drain_exempt``): it can never drain, so waiting on it is
        # pointless. Admitted work that starts waiting on a recorded lock meanwhile ends this
        # wait unfinished: the generation is then engaged and the tail drains it, or, when that
        # work has already stopped waiting, the drain resumes within what is left of the budget.
        drain_unfinished = await old._drain_in_flight(max(0.0, drain_ends - loop.time()), until_gate_wait=True)
        deferred = old.engaged

    await _drain_generation_supervisors(old, budget)

    # Read again after the run drain: admitted work of this generation may have started a
    # recorded wait while it ran.
    deferred = deferred or old.engaged

    # Drop every settings cache and settings-derived singleton the retired generation
    # left behind.
    reset_all_settings()

    if deferred:
        # The tail and its done-callback start from an empty context: either would
        # otherwise copy the driving work's, and with it keep the retired generation
        # reachable after the retire. The tail is still the rebuild's own work, so it
        # starts from the fork gate's rebuild context: a fork it makes while that rebuild
        # holds the gate does not wait on it. It outlives the rebuild and the job that may
        # have driven it, so the context carries no job span of that job's.
        task = asyncio.create_task(
            _deferred_drain_and_close(old, retired, budget),
            name=f"tai-epoch-{old.number}-deferred-retire",
            context=fork_gate.rebuild_context(),
        )
        _deferred_retire_tasks.add(task)
        task.add_done_callback(_on_deferred_retire_done, context=contextvars.Context())
    else:
        await _close_generation(old, retired, budget)
        arm_retired_generation_certification(old, retired)


async def _drain_generation_supervisors(old: Epoch, budget: float) -> None:
    """Drain, within ``budget``, the background runs ``old`` admitted that neither drive nor wait on the gate.

    A run admitted on the fresh epoch during the retire is left running, as is a run that drives
    or waits on the gate. A failure of the drain is logged loudly and the retire proceeds.
    """
    from tai42_skeleton.operations.tool_runs import drain_supervisors

    try:
        await drain_supervisors(
            budget,
            epoch=old.number,
            reason="the serving epoch was retired by a config apply before the tool-run completed",
            exclude={*old.driving_tasks, *old.waiting_tasks},
        )
    except Exception:
        logger.exception("epoch %d retire: background-run drain failed", old.number)


def _on_deferred_retire_done(task: asyncio.Task[None]) -> None:
    """Drop the finished tail's strong reference, and log its failure at ERROR with its traceback.

    Nobody awaits the tail, so a failure it ends with is surfaced here. A cancelled tail stays
    silent: no code of this process cancels a tail, so its cancel comes from the loop being torn
    down, and is that teardown's own, not a failure of the tail. Such a tail may leave the
    generation closed only in part, and its certification is not armed.
    """
    _deferred_retire_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("deferred retire %r failed", task.get_name(), exc_info=exc)


async def _close_generation(old: Epoch, retired: int, budget: float) -> None:
    """Close the retired generation: its transports, then its retired client pools, then its loop-bound registries.

    The transport close runs the FastMCP lifespan's ``__aexit__``, which terminates every live
    session and drops its session manager, so the fresh epoch serves a NEW session-id space and
    stateful clients re-initialise — no handoff. The pool drain then waits, within the drain
    budget, for the leases the generation's work still holds on its client pools, and closes
    them. Last, the retired epoch's loop-bound registries (checkpoint savers, stores) of the
    serving loop are closed and dropped. The close follows the bounded drain: the
    generation's admitted work has ended or the drain budget ran out (logged loudly), so work
    still running past the budget may find its registries closed. Each step runs even when
    another fails, and each failure is logged loudly.
    """
    try:
        await old.close_transports()
    except Exception:
        logger.exception("epoch %d retire: serving-lifespan close failed", old.number)
    try:
        await drain_epoch(retired, budget)
    except Exception:
        logger.exception("epoch %d retire: client-pool drain failed", retired)
    try:
        await close_retired_registries(retired)
    except Exception:
        logger.exception("epoch %d retire: loop-bound registry close failed", retired)


async def _deferred_drain_and_close(old: Epoch, retired: int, budget: float) -> None:
    """Background tail of an engaged generation's retire.

    First lets the reload gate turn over: all admitted work waiting on it until then has
    acquired it (a driver of its own gated body is from then on driving; a pass-through is
    ordinary in-flight work again). Then drains the ordinary admitted work within the
    drain budget (loud past it) and waits for all admitted work and every supervisor driving a gated
    body of this generation to finish — their responses flush on the still-live
    transports, and their own bounds bound the wait. While work of this generation still
    waits on a recorded lock (the gate behind another reload, or a lock whose holder is no
    work of this generation), the tail waits for those waits to end, without holding the loop,
    and repeats. Once no wait is left, the generation's background runs that waited are ordinary
    work: they are drained within the drain budget, as the synchronous retire drains its own.
    Only then are the transports closed, the retired client pools drained and closed, the
    retired loop-bound registries closed, and the retired generation's certification armed.
    Runs on the serving loop (the lifespans' owner loop), so the close stays loop-correct.
    """
    from tai42_skeleton.app.reload_gate import reload_gate

    try:
        while True:
            async with reload_gate.synchronized():
                pass
            await old._drain_in_flight(budget)
            await old._wait_driving()
            if not old.gate_waiters:
                break
            await old._wait_gate_waiters()
        await _drain_generation_supervisors(old, budget)
    finally:
        await _close_generation(old, retired, budget)
    arm_retired_generation_certification(old, retired)
