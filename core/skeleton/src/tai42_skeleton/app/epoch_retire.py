"""Retiring a serving generation once a build swapped a fresh one in.

The retire cancels the old generation's periodic loops, drains its in-flight requests and
background runs, closes its FastMCP lifespan and its retired client pools, resets the settings
caches, and arms the generation's certification
(:func:`~tai42_skeleton.app.retired_generations.arm_retired_generation_certification`). Every
step is bounded by the drain budget. A door-driven reload defers the two request-severing steps
to a background task so the driving request can still deliver its response.
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

# Strong references to the deferred door-driven-retire tasks, so the loop does not GC a
# still-running one; each removes itself on completion.
_deferred_retire_tasks: set[asyncio.Task[None]] = set()


def _drain_budget(deadline: float | None) -> float:
    # ``deadline`` here is a relative duration in SECONDS (the drain budget), not an
    # absolute time — mirrors the kit's ``drain_epoch(epoch, deadline)`` vocabulary.
    if deadline is not None:
        return deadline
    from tai42_skeleton.app import instance

    return instance.build_app().drain_budgets.budget()


async def retire_epoch(old: Epoch, retired: int, deadline: float | None, *, tolerate_driver: bool = False) -> None:
    """Retire the previous generation, bounded by the drain budget.

    Cancels the generation's periodic loops first (no timer outlives its epoch),
    drains its in-flight requests and background supervisors, resets the settings,
    closes its retired client pools, and — once its in-flight requests are drained and
    its lifespan closed — arms its certification: the generation's settings are swept
    for stale-config leaks once its serving surface is collected. Each step is
    independent so one failure cannot skip the rest — the fresh epoch already serves new
    traffic, so a retire fault is loud but never fatal.

    ``tolerate_driver`` marks a door-driven reload: it runs the swap synchronously INSIDE
    the request that drove it, and if that request is an MCP tool call it is served by THIS
    epoch's FastMCP session manager (``old.supervisor``). Draining/``aclose``-ing that here —
    before the tool returns — would sever the transport delivering the tool's own response,
    hanging the client. So for a door-driven reload the two request-severing steps (the
    in-flight drain and the session-manager close) are DEFERRED to a background task on the
    serving loop: ``build_and_swap`` returns at once, the driver finishes and flushes its
    response on the still-live session manager, then the task drains the old epoch and
    ``aclose``s it (for every OTHER session, a beat later, off the reload's hot path —
    so long-lived streamable-http streams no longer gate reload latency either), and the
    certification is armed after the drain there too. A bus-driven reload
    (``tolerate_driver=False``) serves no in-flight request that needs its response
    delivered, so it drains, closes and arms the certification synchronously.
    """
    budget = _drain_budget(deadline)
    await old._cancel_periodic_loops(budget)
    if not tolerate_driver:
        # Drain this generation's in-flight requests SYNCHRONOUSLY before the session-manager
        # ``aclose`` below, so a real request (an MCP tool call, a sync REST tool run) finishes
        # on the old epoch and is never severed mid-response. A long-lived stream that never
        # completes — the interactions-inbox SSE the Studio shell holds — would otherwise burn
        # the FULL budget here (stalling a fleet-sibling's reload ack and fleet convergence), so
        # such a stream EXEMPTS itself from this drain (``mark_current_request_drain_exempt``):
        # it is a plain Starlette route ``aclose`` does NOT sever and can never drain, and it
        # self-terminates on client disconnect, so waiting on it is pointless. The drain thus
        # waits only on real in-flight work, never on the SSE.
        await old._drain_in_flight(budget)

    from tai42_skeleton.operations.tool_runs import drain_supervisors

    try:
        # Drain ONLY this generation's in-flight runs — a run admitted on the fresh
        # epoch during the retire is left running.
        await drain_supervisors(
            budget,
            epoch=old.number,
            reason="the serving epoch was retired by a config apply before the tool-run completed",
        )
    except Exception:
        logger.exception("epoch %d retire: background-run drain failed", old.number)

    # Close the retired generation's FastMCP lifespan: its ``__aexit__`` terminates
    # every live transport and drops its session manager, so the fresh epoch serves a
    # NEW session-id space and stateful clients re-initialise — no handoff. For a
    # door-driven reload this is deferred (see below), so the driver's own transport
    # survives long enough to deliver the tool result.
    if not tolerate_driver and old.supervisor is not None:
        try:
            await old.supervisor.aclose()
        except Exception:
            logger.exception("epoch %d retire: serving-lifespan close failed", old.number)

    # Drop every settings cache and settings-derived singleton the retired generation
    # left behind.
    reset_all_settings()

    try:
        await drain_epoch(retired, budget)
    except Exception:
        logger.exception("epoch %d retire: client-pool drain failed", retired)

    if tolerate_driver:
        # Defer the driver-severing drain + session-manager close so the reload returns now
        # and the driving request flushes its response on the still-live session manager.
        # The task and its done-callback start from an empty context: either would
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
    """Background tail of a door-driven reload's retire.

    Waits (bounded by the drain budget) for the old generation's in-flight
    requests — including the driver, whose response then flushes on the
    still-live session manager — to finish, then ``aclose`` its FastMCP lifespan
    (for every remaining/streaming session) and arms the retired generation's
    certification. Runs on the serving loop (the supervisor's owner loop), so the
    lifespan close stays loop-correct.
    """
    try:
        await old._drain_in_flight(budget)
    finally:
        if old.supervisor is not None:
            try:
                await old.supervisor.aclose()
            except Exception:
                logger.exception("epoch %d retire: deferred serving-lifespan close failed", old.number)
    arm_retired_generation_certification(old, retired)
