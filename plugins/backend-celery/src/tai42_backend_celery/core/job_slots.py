"""The jobs of a celery worker whose pool cannot end a job at its budget, and the progress they report.

The ``solo`` and ``threads`` pools run a job until it returns: they pass no time limit to the
job, so ``CELERY_TASK_TIMEOUT`` ends nothing there. A job that has run past that budget holds a
wedged slot. Once every slot of the worker holds one, :func:`check_wedged` raises, the worker's
progress tick withholds its report, and the liveness probe ends the worker. One wedged thread
beside working ones keeps the worker reporting, because ending the worker would end the healthy
jobs too.

On ``solo`` the job runs on the hub thread, so the hub's timer cannot tick while it runs; the
job's own asyncio loop ticks the worker's progress instead, once a second, through the same tick.
A job that blocks that loop stalls those ticks, and the file turns stale by its horizon.

Every task body enters through :func:`in_job` (``AsyncTask.run_async``). Outside a watched
process (no :func:`watch`, or a prefork child forked from the worker) it runs the job unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Seconds between two progress ticks a running solo job makes: the hub timer's own period.
_TICK_SECONDS = 1.0


@dataclass
class _Watch:
    """The jobs running in one worker process, and how its pool treats them."""

    pid: int
    pool_name: str
    slots: int
    budget: float
    ticks_in_job: bool
    tick: Callable[[], None]
    lock: threading.Lock = field(default_factory=threading.Lock)
    # token -> (label, monotonic start)
    jobs: dict[object, tuple[str, float]] = field(default_factory=dict)


_watch: _Watch | None = None


def watch(*, pool_name: str, slots: int, budget: float, ticks_in_job: bool, tick: Callable[[], None]) -> None:
    """Count this process's jobs against ``slots``, each bounded by ``budget`` seconds.

    Called by the progress step on each consumer start. ``ticks_in_job`` makes a running job call
    ``tick`` once a second. A second call in the same process keeps the jobs already running.
    """
    global _watch
    pid = os.getpid()
    previous = _watch if _watch is not None and _watch.pid == pid else None
    current = _Watch(pid=pid, pool_name=pool_name, slots=slots, budget=budget, ticks_in_job=ticks_in_job, tick=tick)
    if previous is not None:
        current.lock = previous.lock
        current.jobs = previous.jobs
    _watch = current


def check_wedged() -> None:
    """Raise ``RuntimeError`` when every slot of this worker holds a job past its budget."""
    current = _watch
    if current is None or current.pid != os.getpid():
        return
    now = time.monotonic()
    with current.lock:
        overdue = sorted(label for label, start in current.jobs.values() if now - start > current.budget)
    if len(overdue) >= current.slots:
        raise RuntimeError(
            f"every job slot of this worker holds a job past CELERY_TASK_TIMEOUT={current.budget:g}s on the "
            f"{current.pool_name} pool, which cannot end a job; reporting stops so the probe ends this worker: "
            f"{', '.join(overdue)}"
        )


async def in_job(coroutine: Coroutine[Any, Any, Any], *, label: str) -> Any:
    """Run ``coroutine`` as one job of this worker, named ``label``; return or raise what it does."""
    current = _watch
    if current is None or current.pid != os.getpid():
        return await coroutine
    token = object()
    with current.lock:
        current.jobs[token] = (label, time.monotonic())
    try:
        if not current.ticks_in_job:
            return await coroutine
        job = asyncio.ensure_future(coroutine)
        ticker = asyncio.ensure_future(_tick_until_done(job, current.tick, label))
        try:
            return await job
        finally:
            # The ticker returns once the job is done, cancelled or not.
            await ticker
    finally:
        with current.lock:
            del current.jobs[token]


async def _tick_until_done(job: asyncio.Future[Any], tick: Callable[[], None], label: str) -> None:
    """Tick the worker's progress once a second until ``job`` is done; a tick that raises ends the ticks."""
    while not job.done():
        try:
            tick()
        except Exception:
            logger.exception("the progress tick during job %s failed; this job reports no more progress", label)
            return
        await asyncio.wait({job}, timeout=_TICK_SECONDS)
