"""The celery worker's progress: a consumer bootstep that reports each tick of the hub timer.

The worker is alive while its own loop passes and every process or job it cannot end makes
progress too, so each tick, before it reports:

- reads the embedded beat's liveness file when the worker runs one (``-B``,
  :mod:`~tai42_backend_celery.core.embedded_beat`), and withholds the report while the beat's
  promise is overdue;
- on a pool that cannot end a job at its budget (``solo``, ``threads``, ``eventlet``), withholds the report once
  a job runs past ``CELERY_TASK_TIMEOUT`` (:mod:`~tai42_backend_celery.core.job_slots`).
  On ``solo`` the job runs on the hub thread, so the running job makes this same tick itself.

The same tick reports while a fork or an in-process job waits for a config reload's re-import
(the fork gate's guard, installed by the worker runtime's build, and
:func:`~tai42_backend_celery.core.job_slots.in_process_job`).

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

from dataclasses import dataclass
from typing import Any, cast

from celery import Celery, bootsteps
from celery.worker.consumer.connection import Connection
from tai42_contract.backend.runtime import ProgressReporter

from tai42_backend_celery.core import embedded_beat, job_slots

# Seconds between two ticks of the step, and so the promise of each report. The liveness file
# throttles its writes to its own interval, so a faster tick costs one clock read.
PROGRESS_TICK_SECONDS = 1.0


@dataclass(frozen=True)
class _Pool:
    """How a celery pool runs its jobs, under the name the docs use."""

    name: str
    # Its jobs run in the worker process itself, not in forked children.
    in_process: bool
    # It ends a job at its time limit; the others run a job until it returns (the eventlet
    # pool takes no time limit at all).
    ends_jobs: bool


# Every pool celery ships, keyed by the module of its class: celery names its pools by module
# too, and the green pools are named without importing their optional extras.
_POOLS: dict[str, _Pool] = {
    "celery.concurrency.prefork": _Pool("prefork", in_process=False, ends_jobs=True),
    "celery.concurrency.solo": _Pool("solo", in_process=True, ends_jobs=False),
    "celery.concurrency.thread": _Pool("threads", in_process=True, ends_jobs=False),
    "celery.concurrency.gevent": _Pool("gevent", in_process=True, ends_jobs=True),
    "celery.concurrency.eventlet": _Pool("eventlet", in_process=True, ends_jobs=False),
}

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


def tick() -> None:
    """Report one pass of the worker, promising the next within one tick, unless what it cannot end is stuck."""
    if _report is None:
        raise RuntimeError(
            "the celery worker's progress step ticked with no reporter registered: register() runs from "
            "the worker runtime's build"
        )
    embedded_beat.check()
    job_slots.check_wedged()
    _report(next_within=PROGRESS_TICK_SECONDS)


class ProgressStep(bootsteps.StartStopStep):
    """Reports one pass of the hub loop per tick of the consumer's timer; registered on every consumer start."""

    requires = (Connection,)

    def __init__(self, parent: Any, **kwargs: Any) -> None:
        """Start with no timer entry: :meth:`start` registers it."""
        self.tref: Any = None
        super().__init__(parent, **kwargs)

    def start(self, parent: Any) -> None:
        """Watch the pool's jobs, then register the repeating tick.

        A pool whose class is not one celery ships raises ``RuntimeError``: how it runs its jobs,
        and so how to report for them, is unknown.
        """
        module = type(parent.pool).__module__
        pool = _POOLS.get(module)
        if pool is None:
            raise RuntimeError(
                f"the celery worker's pool {type(parent.pool).__qualname__} from {module} is not one celery ships "
                f"({', '.join(entry.name for entry in _POOLS.values())}); its progress cannot be reported"
            )
        job_slots.watch(
            pool_name=pool.name,
            in_process=pool.in_process,
            budget=None if pool.ends_jobs else _task_timeout,
            ticks_in_job=pool.name == "solo",
            tick=tick,
        )
        self.tref = parent.timer.call_repeatedly(PROGRESS_TICK_SECONDS, tick)

    def stop(self, parent: Any) -> None:
        """Cancel the tick, so a restarted consumer registers exactly one."""
        if self.tref is not None:
            self.tref.cancel()
            self.tref = None

    shutdown = stop
