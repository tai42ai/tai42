"""The jobs running in one celery worker process, and how its pool treats them.

A pool has two properties here. Whether its jobs run in the worker process itself (``solo``,
``threads``, ``gevent``, ``eventlet``) or in forked children (``prefork``): a job in the worker
process imports against the same live ``sys.modules`` a config reload re-imports, so
:func:`in_process_job` holds the fork gate's job span for its whole run. And whether the pool can
end a job at its budget: the ``solo``, ``threads`` and ``eventlet`` pools run a job until it
returns, so ``CELERY_TASK_TIMEOUT`` ends nothing there. On those, once one job runs past that budget,
:func:`check_wedged` raises, the worker's progress tick withholds its report, and the liveness
probe ends the worker: the job holds its slot for good, and a thread cannot be ended on its own.

On ``solo`` the job runs on the hub thread, so the hub's timer cannot tick while it runs; the
job's own asyncio loop ticks the worker's progress instead, once a second, through the same tick.
A job that blocks that loop stalls those ticks, and the file turns stale by its horizon.

Every task body enters through :func:`in_process_job` and :func:`in_job` (``AsyncTask.run_async``).
Outside a watched process (no :func:`watch`, or a prefork child forked from the worker) both run
the job unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from tai42_kit.fork_gate import JOB_SPAN_WAIT_SECONDS, fork_gate

logger = logging.getLogger(__name__)

# Seconds between two progress ticks a running solo job, or a job waiting on the fork gate, makes:
# the hub timer's own period.
_TICK_SECONDS = 1.0


@dataclass
class _Watch:
    """The jobs running in one worker process, and how its pool treats them."""

    pid: int
    pool_name: str
    # The pool runs its jobs in this process, not in forked children.
    in_process: bool
    # The job budget a job is held to; ``None`` on a pool that ends a job at its budget itself.
    budget: float | None
    ticks_in_job: bool
    tick: Callable[[], None]
    lock: threading.Lock = field(default_factory=threading.Lock)
    # token -> (label, monotonic start)
    jobs: dict[object, tuple[str, float]] = field(default_factory=dict)


_watch: _Watch | None = None


def watch(
    *, pool_name: str, in_process: bool, budget: float | None, ticks_in_job: bool, tick: Callable[[], None]
) -> None:
    """Watch this process's jobs on the pool named ``pool_name``.

    Called by the progress step on each consumer start, for every pool. ``in_process`` makes each
    job hold the fork gate's job span (:func:`in_process_job`), and a job waiting on it calls
    ``tick`` once a second. ``budget`` bounds each job in seconds on a pool that cannot end a job
    itself (``None`` elsewhere); ``ticks_in_job`` makes a running job call ``tick`` once a second.
    A second call in the same process keeps the jobs already running.
    """
    global _watch
    pid = os.getpid()
    previous = _watch if _watch is not None and _watch.pid == pid else None
    current = _Watch(
        pid=pid, pool_name=pool_name, in_process=in_process, budget=budget, ticks_in_job=ticks_in_job, tick=tick
    )
    if previous is not None:
        current.lock = previous.lock
        current.jobs = previous.jobs
    _watch = current


def check_wedged() -> None:
    """Raise ``RuntimeError`` when a job of this worker runs past its budget on a pool that cannot end it."""
    current = _watch
    if current is None or current.pid != os.getpid() or current.budget is None:
        return
    budget = current.budget
    now = time.monotonic()
    with current.lock:
        overdue = sorted(label for label, start in current.jobs.values() if now - start > budget)
    if overdue:
        raise RuntimeError(
            f"a job slot of this worker holds a job past CELERY_TASK_TIMEOUT={budget:g}s on the "
            f"{current.pool_name} pool, which cannot end it; reporting stops so the probe ends this worker: "
            f"{', '.join(overdue)}"
        )


@contextmanager
def in_process_job() -> Iterator[None]:
    """Hold the fork gate's job span for a job that runs in this worker process.

    Its imports race a config reload's re-import for its whole run. While the span waits for a
    running re-import, the worker's progress is ticked once a second. A job in a prefork child takes
    no span: the child's gate is empty and no re-import runs there.
    """
    current = _watch
    if current is None or current.pid != os.getpid() or not current.in_process:
        yield
        return
    with fork_gate.job_span(timeout=JOB_SPAN_WAIT_SECONDS, tick=current.tick, tick_every=_TICK_SECONDS):
        yield


async def in_job(coroutine: Coroutine[Any, Any, Any], *, label: str) -> Any:
    """Run ``coroutine`` as one job of this worker, named ``label``; return or raise what it does."""
    current = _watch
    if current is None or current.pid != os.getpid() or current.budget is None:
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
