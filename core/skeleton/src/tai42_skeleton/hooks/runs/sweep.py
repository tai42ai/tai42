"""The per-process sweep of hook runs: claim the due jobs and those whose lease lapsed, run each one.

Every ``HOOKS_RUN_SWEEP_SECONDS`` a tick claims, up to this process's free capacity, the jobs whose
score is due: pending jobs whose next attempt is due and running jobs whose lease lapsed. The claim
is one atomic step in the store, so concurrent sweeps of several processes never claim the same
job. Every process that runs the app lifespan sweeps.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from . import runtime
from .metrics import hook_run_metrics

logger = logging.getLogger(__name__)

_sweep_task: asyncio.Task[None] | None = None


async def sweep_tick() -> None:
    """One sweep tick: claim the due jobs this process has room for, spawn their attempts, set the gauge."""
    mgr = runtime.manager()
    if not runtime.stopping():
        free = max(0, mgr.settings.max_workers - runtime.attempt_count())
        if free:
            now = runtime.now()
            claimed = await mgr._claim_due_runs(
                now,
                free,
                runtime.claimed_by(),
                now + mgr.settings.run_lease_seconds,
                runtime.live_attempt_ids(),
            )
            for job in claimed:
                runtime.spawn_attempt(job)
    counts = await mgr.count_hook_runs()
    metrics = hook_run_metrics()
    metrics.runs.labels(status="outstanding").set(counts.outstanding)
    metrics.runs.labels(status="failed").set(counts.failed)


async def _sweep_loop(interval: float) -> None:
    """Run a tick every ``interval`` seconds for the life of the generation; a failing tick is logged and counted."""
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep_tick()
        except Exception:
            hook_run_metrics().sweep_errors.inc()
            logger.error("hook runs: sweep tick failed; retrying in %ss", interval, exc_info=True)


def _on_sweep_done(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("hook runs: the sweep task died unexpectedly", exc_info=exc)


def start_hook_runs_sweep() -> None:
    """(Re)start the sweep on the serving loop and register its cancel with the epoch under construction.

    It also clears the runtime's stopping flag, so a lifespan that starts again serves again.
    """
    global _sweep_task
    runtime.set_stopping(False)
    interval = runtime.manager().settings.run_sweep_seconds
    previous = _sweep_task
    if previous is not None and not previous.done():
        previous.cancel()
    logger.info("hook runs: sweeping due hook runs every %ss", interval)
    task = asyncio.create_task(_sweep_loop(interval), name="tai-hook-runs-sweep")
    task.add_done_callback(_on_sweep_done)
    _sweep_task = task
    from tai42_skeleton.app.epoch import epoch_under_construction_or_none

    epoch = epoch_under_construction_or_none()
    if epoch is None:
        return

    async def _cancel() -> None:
        if task.done():
            return
        task.cancel()
        if task.get_loop() is asyncio.get_running_loop():
            with contextlib.suppress(asyncio.CancelledError):
                await task

    epoch.register_periodic_loop(_cancel)


async def stop_hook_runs_sweep() -> None:
    """Stop the sweep, give every attempt of this process the shutdown grace, then cancel what is left.

    From the moment it begins, an accept in this process writes its jobs unclaimed and due now and
    spawns no attempt. Each cancelled attempt frees its job by the interrupted-attempt rule: it is
    released for the next process, or held as failed. An attempt still running one more grace
    after its cancel is logged at ERROR and cancelled again. One still running a grace after
    that is logged at ERROR and left in flight: the stop returns, and the lease reclaims its job.
    """
    global _sweep_task
    runtime.set_stopping(True)
    task = _sweep_task
    _sweep_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    grace = runtime.manager().settings.run_shutdown_grace_seconds
    still = await runtime.drain_hook_runs(timeout=grace)
    if not still:
        return
    for left in still:
        left.cancel()
    _ended, hung = await asyncio.wait(still, timeout=grace)
    if hung:
        logger.error(
            "hook runs: %d hook-run attempt(s) did not end within %ss of their shutdown cancel; cancelling them "
            "again: %s",
            len(hung),
            grace,
            sorted(t.get_name() for t in hung),
        )
        for left in hung:
            left.cancel()
        _ended, in_flight = await asyncio.wait(hung, timeout=grace)
        if in_flight:
            logger.error(
                "hook runs: %d hook-run attempt(s) still in flight %ss after their second cancel; the stop goes on "
                "and their jobs are left to the lease: %s",
                len(in_flight),
                grace,
                sorted(t.get_name() for t in in_flight),
            )
            return
    logger.warning(
        "hook runs: shutdown cancelled %d hook-run attempt(s) after %ss: %s",
        len(still),
        grace,
        sorted(t.get_name() for t in still),
    )


__all__ = ["start_hook_runs_sweep", "stop_hook_runs_sweep", "sweep_tick"]
