"""The per-process recovery sweep of pending saves.

Every ``STATES_OUTBOX_SWEEP_SECONDS`` a pass applies the due ``pending`` rows, runs the calls of the
due ``calls`` rows and of the ``running`` rows whose claim lapsed, and sets the gauges. Every
process that runs the app lifespan runs one; the advisory locks and the calls claim make concurrent
sweeps safe, so any process recovers any row.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from tai42_skeleton.states import db as states_db
from tai42_skeleton.states.db import states_settings

from .apply import apply_row, in_flight_tasks, run_calls
from .drain import live_states_service
from .metrics import outbox_metrics

logger = logging.getLogger(__name__)

_BATCH = 100
_STATUSES = ("pending", "calls", "running", "failed")

_sweep_task: asyncio.Task[None] | None = None


async def sweep_pass() -> None:
    """One recovery pass over the outbox."""
    service = live_states_service()
    store = service._store
    for row_id in await store.outbox_due_pending(_BATCH):
        await apply_row(service, row_id)
    for row_id in await store.outbox_due_calls(_BATCH):
        await run_calls(service, row_id)
    counts: dict[str, tuple[int, datetime | None]] = dict.fromkeys(_STATUSES, (0, None))
    for status, rows, oldest in await store.outbox_status_counts():
        counts[status] = (rows, oldest)
    metrics = outbox_metrics()
    for status, (rows, _oldest) in counts.items():
        metrics.rows.labels(status).set(rows)
    oldest = [o for _rows, o in counts.values() if o is not None]
    metrics.oldest_age_seconds.set((datetime.now(UTC) - min(oldest)).total_seconds() if oldest else 0.0)


async def _sweep_loop(interval: float) -> None:
    """Run a pass every ``interval`` seconds for the life of the generation; a failing pass is logged and counted."""
    while True:
        await asyncio.sleep(interval)
        try:
            await sweep_pass()
        except Exception:
            outbox_metrics().sweep_errors.inc()
            logger.error("states outbox: sweep pass failed; retrying in %ss", interval, exc_info=True)


def _on_sweep_done(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("states outbox: the sweep task died unexpectedly", exc_info=exc)


def start_state_outbox_sweep() -> None:
    """(Re)start the sweep on the serving loop and register its cancel with the epoch under construction.

    Reads the outbox settings, so a bad setting pair fails the boot loudly. A no-op while the
    states feature is off.
    """
    global _sweep_task
    if not states_db.states_store_configured():
        return
    interval = states_settings().outbox_sweep_seconds
    previous = _sweep_task
    if previous is not None and not previous.done():
        previous.cancel()
    logger.info("states outbox: sweeping pending saves every %ss", interval)
    task = asyncio.create_task(_sweep_loop(interval), name="tai-states-outbox-sweep")
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


async def stop_state_outbox_sweep() -> None:
    """Cancel the sweep, give the in-flight applies the shutdown grace, then cancel and name what is left."""
    global _sweep_task
    task = _sweep_task
    _sweep_task = None
    if task is not None and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    pending = in_flight_tasks()
    if not pending:
        return
    grace = states_settings().outbox_shutdown_grace_seconds
    _done, still = await asyncio.wait(pending, timeout=grace)
    if not still:
        return
    for left in still:
        left.cancel()
    await asyncio.gather(*still, return_exceptions=True)
    logger.warning(
        "states outbox: shutdown cancelled %d in-flight pending-save task(s) after %ss: %s",
        len(still),
        grace,
        sorted(t.get_name() for t in still),
    )
