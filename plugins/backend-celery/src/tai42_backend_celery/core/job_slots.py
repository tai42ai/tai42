"""The jobs running in one celery worker process, and how its pool treats them.

A pool has three properties here. Whether its jobs run in the worker process itself (``solo``,
``threads``, ``gevent``, ``eventlet``) or in forked children (``prefork``), which
:func:`in_process_pool` answers: a job in the worker process imports against the same live
``sys.modules`` a config reload re-imports, so :func:`in_process_job` holds the fork gate's job span
for its whole run, and the worker process itself shuts its jobs' work down
(:func:`~tai42_backend_celery.core.worker_signals.shut_down_jobs`). And what the pool does with
a job at its budget: the ``solo``, ``threads`` and ``eventlet`` pools run a job until it returns,
so ``CELERY_TASK_TIMEOUT`` ends nothing there; the ``gevent`` pool ends the job's wait at
``CELERY_TASK_TIMEOUT`` (and on a revoke) and cancels its async work, which the job then waits for.
On every one of them, once a job still runs a tick past that budget, :func:`check_wedged` raises,
the worker's progress tick withholds its report, and the liveness probe ends the worker: the job
holds its slot for good and this pool cannot end its work. A revoke with terminate is refused by a
``threads`` worker, whose pool cannot end a thread, and is read by a ``solo`` worker only once the
job has ended. And whether its jobs are green threads of one OS thread (``gevent``, ``eventlet``),
which can run one event loop: :func:`green_pool` answers it, and the process's jobs, whatever their
task, then share the one loop, the host's (:mod:`~tai42_backend_celery.core.task_loops`).

On ``solo`` the job runs on the hub thread, so the hub's timer cannot tick while it runs; the
job's own asyncio loop ticks the worker's progress instead, once a second, through the same tick.
A job that blocks that loop stalls those ticks, and the file turns stale by its horizon.

The watch lives on the app the worker runs (``HostedCelery.job_watch``), which every task reaches
as its own ``app``: a config reload re-imports this module while the worker runs, and the worker's
jobs still find the watch its progress step set. Every task body enters through
:func:`in_process_job` and :func:`in_job` (``AsyncTask.run_async``). Outside a watched process (no
:func:`watch` on the app, or a prefork child forked from the worker) both run the job unchanged.
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
from typing import TYPE_CHECKING, Any

from tai42_kit.fork_gate import JOB_SPAN_WAIT_SECONDS, fork_gate

if TYPE_CHECKING:
    from tai42_backend_celery.core.app import HostedCelery

logger = logging.getLogger(__name__)

# Seconds between two ticks of the worker's progress, and so the promise of each report: the
# period of the progress step's hub timer, of a running solo job's own ticks and of a job's ticks
# while it waits on the fork gate. The liveness file throttles its writes to its own interval, so
# a faster tick costs one clock read. A job still running one tick past its budget is wedged: the
# gevent pool's cancel at the budget reaches the job's work well within one tick.
PROGRESS_TICK_SECONDS = 1.0


@dataclass
class JobWatch:
    """The jobs running in one worker process, and how its pool treats them."""

    pid: int
    pool_name: str
    # The pool runs its jobs in this process, not in forked children.
    in_process: bool
    # The pool's jobs are green threads of one OS thread, which can run one event loop.
    green: bool
    # The job budget, in seconds, every job of an in-process pool is held to.
    budget: float
    ticks_in_job: bool
    tick: Callable[[], None]
    lock: threading.Lock = field(default_factory=threading.Lock)
    # token -> (label, monotonic start)
    jobs: dict[object, tuple[str, float]] = field(default_factory=dict)


def _this_process_watch(app: HostedCelery) -> JobWatch | None:
    """The watch of this process's pool on ``app``, or ``None`` when none is watched here.

    A watch set in another process (the worker a prefork child or the ``-B`` beat was forked from,
    whose app the child inherits) is not this process's.
    """
    current = app.job_watch
    return current if current is not None and current.pid == os.getpid() else None


def watch(
    app: HostedCelery,
    *,
    pool_name: str,
    in_process: bool,
    green: bool,
    budget: float,
    ticks_in_job: bool,
    tick: Callable[[], None],
) -> None:
    """Watch this process's jobs of ``app`` on the pool named ``pool_name``.

    Called by the progress step on each consumer start, for every pool. ``in_process`` makes each
    job hold the fork gate's job span (:func:`in_process_job`). ``green`` says the jobs are green
    threads of one OS thread (:func:`green_pool`). ``budget`` bounds each job in seconds
    (:func:`check_wedged`); ``ticks_in_job`` makes a running job call ``tick`` once a second.
    A second call in the same process keeps the jobs already running.
    """
    previous = _this_process_watch(app)
    current = JobWatch(
        pid=os.getpid(),
        pool_name=pool_name,
        in_process=in_process,
        green=green,
        budget=budget,
        ticks_in_job=ticks_in_job,
        tick=tick,
    )
    if previous is not None:
        current.lock = previous.lock
        current.jobs = previous.jobs
    app.job_watch = current


def in_process_pool(app: HostedCelery) -> bool:
    """Whether this process's pool of ``app`` runs its jobs in this process; ``False`` when none is watched."""
    current = _this_process_watch(app)
    return current is not None and current.in_process


def green_pool(app: HostedCelery) -> bool:
    """Whether this process's pool of ``app`` runs its jobs as green threads of one OS thread.

    ``False`` when none is watched.
    """
    current = _this_process_watch(app)
    return current is not None and current.green


def check_wedged(app: HostedCelery) -> None:
    """Raise ``RuntimeError`` when a job of this worker of ``app`` still runs a tick past its budget.

    A pool that ends a job's wait at its budget (``gevent``) has cancelled its work by then, so work
    that still runs did not end on the cancel; the other in-process pools end nothing.
    """
    current = _this_process_watch(app)
    if current is None:
        return
    budget = current.budget
    now = time.monotonic()
    with current.lock:
        overdue = sorted(
            label for label, start in current.jobs.values() if now - start > budget + PROGRESS_TICK_SECONDS
        )
    if overdue:
        raise RuntimeError(
            f"a job slot of this worker holds a job that still runs a tick past CELERY_TASK_TIMEOUT={budget:g}s "
            f"on the {current.pool_name} pool, which cannot end it; reporting stops so the probe ends this worker: "
            f"{', '.join(overdue)}"
        )


@contextmanager
def in_process_job(app: HostedCelery) -> Iterator[None]:
    """Hold the fork gate's job span for a job of ``app`` that runs in this worker process.

    Its imports race a config reload's re-import for its whole run. While the span waits for a
    running re-import on the hub's own thread (a ``solo`` job), the worker's progress is ticked once
    a second, as the fork gate's engine loop (the worker runtime's ``run_blocking``); a job waiting
    on a thread of the pool's reports nothing. A job in a prefork child takes no span: the child's
    gate is empty and no re-import runs there.
    """
    current = _this_process_watch(app)
    if current is None or not current.in_process:
        yield
        return
    with fork_gate.job_span(timeout=JOB_SPAN_WAIT_SECONDS):
        yield


async def in_job(app: HostedCelery, coroutine: Coroutine[Any, Any, Any], *, label: str) -> Any:
    """Run ``coroutine`` as one job of this worker of ``app``, named ``label``; return or raise what it does."""
    current = _this_process_watch(app)
    if current is None:
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
        await asyncio.wait({job}, timeout=PROGRESS_TICK_SECONDS)
