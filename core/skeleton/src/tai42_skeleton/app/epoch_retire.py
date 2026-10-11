"""Retiring a serving generation once a build swapped a fresh one in.

The retire cancels the old generation's periodic loops, drains its in-flight requests and
background runs, closes its transports (its FastMCP lifespan and the sub-app lifespans it
adopted) and its retired client pools, resets the settings caches, and arms the generation's
certification
(:func:`~tai42_skeleton.app.retired_generations.arm_retired_generation_certification`).

The drain budget bounds the work that can finish on its own. Work that drives or waits on the
reload gate (recorded on the generation by
:class:`~tai42_skeleton.app.reload_gate.GateParty`) cannot finish before the gate is released,
which happens only after the retire returns: it is never counted against the budget and never
severed. When the generation has such work the retire leaves the transports open and hands the
drain and the close to a background tail that lets the gate turn over, drains the ordinary work
within the budget, waits for every driver to answer, and only then closes.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from typing import TYPE_CHECKING

from tai42_kit.clients import drain_epoch
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.retired_generations import arm_retired_generation_certification

if TYPE_CHECKING:
    from tai42_skeleton.app.epoch import Epoch

logger = logging.getLogger(__name__)

# Strong references to the deferred retire tails, so the loop does not GC a still-running
# one; each removes itself on completion.
_deferred_retire_tasks: set[asyncio.Task[None]] = set()


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
    its in-flight requests and background supervisors, resets the settings, closes its
    retired client pools, and — once its in-flight requests are drained and its
    transports closed — arms its certification: the generation's settings are swept for
    stale-config leaks once its serving surface is collected. Each step is independent
    so one failure cannot skip the rest — the fresh epoch already serves new traffic, so
    a retire fault is loud but never fatal.

    A generation with no work on the reload gate (a bus-delivered reload, a self-resync)
    is drained and closed synchronously. A generation whose requests or supervisors drive
    or wait on the gate is ``engaged``: a reload driven through a door runs this retire
    INSIDE the request that drives it, and an MCP tool call's response travels on this
    generation's session manager, so closing the transports here would sever the reply.
    The retire then returns at once and the background tail
    (:func:`_deferred_drain_and_close`) closes the transports after every driver has
    answered. A supervisor that drives or waits on the gate is never cancelled by the
    supervisor drain.
    """
    budget = _drain_budget(deadline)
    await old._cancel_periodic_loops(budget)
    if not old.engaged:
        # Drain this generation's in-flight requests SYNCHRONOUSLY before the transport
        # close below, so a real request (an MCP tool call, a sync REST tool run) finishes
        # on the old epoch and is never severed mid-response. A long-lived stream that never
        # completes — the interactions-inbox SSE the Studio shell holds — EXEMPTS itself
        # from this drain (``mark_current_request_drain_exempt``): it is a plain Starlette
        # route the transport close does NOT sever and can never drain, and it
        # self-terminates on client disconnect, so waiting on it is pointless.
        await old._drain_in_flight(budget)

    from tai42_skeleton.operations.tool_runs import drain_supervisors

    try:
        # Drain ONLY this generation's in-flight runs — a run admitted on the fresh epoch
        # during the retire is left running, as is a run that drives or waits on the gate.
        await drain_supervisors(
            budget,
            epoch=old.number,
            reason="the serving epoch was retired by a config apply before the tool-run completed",
            exclude=old.driving_tasks | old.waiting_tasks,
        )
    except Exception:
        logger.exception("epoch %d retire: background-run drain failed", old.number)

    # Read again after the drains: a request of this generation may have reached the gate
    # while they ran.
    deferred = old.engaged
    if not deferred:
        # Close the retired generation's transports: the FastMCP lifespan's ``__aexit__``
        # terminates every live session and drops its session manager, so the fresh epoch
        # serves a NEW session-id space and stateful clients re-initialise — no handoff.
        try:
            await old.close_transports()
        except Exception:
            logger.exception("epoch %d retire: serving-lifespan close failed", old.number)

    # Drop every settings cache and settings-derived singleton the retired generation
    # left behind.
    reset_all_settings()

    try:
        await drain_epoch(retired, budget)
    except Exception:
        logger.exception("epoch %d retire: client-pool drain failed", retired)

    if deferred:
        # The tail and its done-callback start from an empty context: either would
        # otherwise copy the driving request's, and with it keep the retired generation
        # reachable after the retire.
        task = asyncio.create_task(
            _deferred_drain_and_close(old, retired, budget),
            name=f"tai-epoch-{old.number}-deferred-retire",
            context=contextvars.Context(),
        )
        _deferred_retire_tasks.add(task)
        task.add_done_callback(_deferred_retire_tasks.discard, context=contextvars.Context())
    else:
        arm_retired_generation_certification(old, retired)


async def _deferred_drain_and_close(old: Epoch, retired: int, budget: float) -> None:
    """Background tail of an engaged generation's retire.

    First lets the reload gate turn over: every request waiting on it when the retire ran
    has then acquired it (a driver of its own gated body is from then on driving; a
    pass-through is ordinary in-flight work again). Then drains the ordinary in-flight
    requests within the drain budget (loud past it), waits for every request and
    supervisor driving a gated body of this generation to finish — their responses flush
    on the still-live transports, and their own bounds bound the wait — and only then
    closes the transports and arms the retired generation's certification. Runs on the
    serving loop (the lifespans' owner loop), so the close stays loop-correct.
    """
    from tai42_skeleton.app.reload_gate import reload_gate

    try:
        async with reload_gate.synchronized():
            pass
        await old._drain_in_flight(budget)
        await old._wait_driving()
    finally:
        try:
            await old.close_transports()
        except Exception:
            logger.exception("epoch %d retire: deferred serving-lifespan close failed", old.number)
    arm_retired_generation_certification(old, retired)
