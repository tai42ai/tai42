"""The celery worker's progress: a consumer bootstep that reports each tick of the hub timer.

The worker is alive while its own loop passes and every process or job it cannot end makes
progress too, so each tick, before it reports:

- reads the embedded beat's liveness file when the worker runs one (``-B``,
  :mod:`~tai42_backend_celery.core.embedded_beat`), and withholds the report while the beat's
  promise is overdue;
- on a pool that cannot end a job at its budget (``solo``, ``threads``), withholds the report once
  every slot holds a job past ``CELERY_TASK_TIMEOUT`` (:mod:`~tai42_backend_celery.core.job_slots`).
  On ``solo`` the job runs on the hub thread, so the running job makes this same tick itself.

On an async transport the worker's hub loop drives the timer and the timer has no thread of
its own, so a tick fires only while the loop passes. The step registers one repeating timer
entry each time the consumer starts, which celery does again after every broker reconnect
(the loop clears its timer entries when the connection is lost). A prefork child that hangs
on a job is the pool's to kill, by the task time limit; on a green pool (``-P gevent`` or
``-P eventlet``) the timer runs on its own green thread, so there a tick proves the process
and that thread rather than the loop.

A tick that raises (a withheld report, an unwritable liveness file, or no reporter registered)
is logged at ERROR by celery's hub and the entry repeats; the file then stops being refreshed,
which is what the probe reads.
"""

from __future__ import annotations

from typing import Any, cast

from celery import Celery, bootsteps
from celery.concurrency import solo, thread
from celery.worker.consumer.connection import Connection
from tai42_contract.backend.runtime import ProgressReporter

from tai42_backend_celery.core import embedded_beat, job_slots

# Seconds between two ticks of the step, and so the promise of each report. The liveness file
# throttles its writes to its own interval, so a faster tick costs one clock read.
_PROGRESS_TICK_SECONDS = 1.0

# The pools that run a job until it returns, whatever its time limit, by the name the docs use.
_UNBOUNDED_POOLS: dict[type[Any], str] = {solo.TaskPool: "solo", thread.TaskPool: "threads"}

_report: ProgressReporter | None = None
_task_timeout: float = 0.0


def register(app: Celery, report: ProgressReporter, *, task_timeout: float) -> None:
    """Report this worker's progress through ``report``, by adding :class:`ProgressStep` to ``app``.

    ``task_timeout`` is the job budget a job on a pool that cannot end it is held to. Worker-scoped,
    called from the worker runtime's ``build``, never at import. A second call with the same
    reporter changes nothing; one with another reporter raises ``RuntimeError``, because one
    process reports through one reporter.
    """
    global _report, _task_timeout
    if _report is not None and _report != report:
        raise RuntimeError("the celery worker already reports its progress through another reporter")
    _report = report
    _task_timeout = task_timeout
    consumer_steps(app).add(ProgressStep)


def consumer_steps(app: Celery) -> set[type[bootsteps.Step]]:
    """The custom bootsteps ``app`` adds to every worker consumer's blueprint.

    celery declares ``steps = None`` on the class and sets a ``defaultdict(set)`` in ``__init__``.
    """
    return cast("dict[str, set[type[bootsteps.Step]]]", app.steps)["consumer"]


def _tick() -> None:
    """Report one pass of the worker, promising the next within one tick, unless what it cannot end is stuck."""
    if _report is None:
        raise RuntimeError(
            "the celery worker's progress step ticked with no reporter registered: register() runs from "
            "the worker runtime's build"
        )
    embedded_beat.check()
    job_slots.check_wedged()
    _report(next_within=_PROGRESS_TICK_SECONDS)


class ProgressStep(bootsteps.StartStopStep):
    """Reports one pass of the hub loop per tick of the consumer's timer; registered on every consumer start."""

    requires = (Connection,)

    def __init__(self, parent: Any, **kwargs: Any) -> None:
        """Start with no timer entry: :meth:`start` registers it."""
        self.tref: Any = None
        super().__init__(parent, **kwargs)

    def start(self, parent: Any) -> None:
        """Watch the pool's jobs, then register the repeating tick."""
        pool_name = next((name for cls, name in _UNBOUNDED_POOLS.items() if isinstance(parent.pool, cls)), None)
        if pool_name is not None:
            job_slots.watch(
                pool_name=pool_name,
                slots=parent.pool.limit,
                budget=_task_timeout,
                ticks_in_job=pool_name == "solo",
                tick=_tick,
            )
        self.tref = parent.timer.call_repeatedly(_PROGRESS_TICK_SECONDS, _tick)

    def stop(self, parent: Any) -> None:
        """Cancel the tick, so a restarted consumer registers exactly one."""
        if self.tref is not None:
            self.tref.cancel()
            self.tref = None

    shutdown = stop
