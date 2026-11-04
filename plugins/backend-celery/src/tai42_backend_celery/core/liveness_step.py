"""The celery worker's progress: a consumer bootstep that reports each tick of the hub timer.

The worker is alive while its own loop passes and every process or job it cannot end makes
progress too, so each tick, before it reports:

- reads the embedded beat's liveness file when the worker runs one (``-B``,
  :mod:`~tai42_backend_celery.core.embedded_beat`), and withholds the report while the beat's
  promise is overdue;
- on an in-process pool (``solo``, ``threads``, ``gevent``, ``eventlet``), withholds the report once a
  job still runs a tick past ``CELERY_TASK_TIMEOUT``: the ``gevent`` pool cancels a job's async work
  at the budget, the others end nothing (:mod:`~tai42_backend_celery.core.job_slots`).
  On ``solo`` the job runs on the hub thread, so the running job makes this same tick itself.

The same tick reports while a fork or an in-process job waits for a config reload's re-import
on the consumer loop's own thread (the fork gate's guard, installed by the worker runtime's build, and
:func:`~tai42_backend_celery.core.job_slots.in_process_job`): the worker runtime runs the consumer
loop (the hub on an async transport) as the fork gate's engine loop, and such a wait holds up
the loop's next pass. A wait on any other
thread (a ``threads`` job's, a fork from a thread of its own) reports nothing: the loop passes or
stalls without it.

On an async transport the worker's hub loop drives the timer and the timer has no thread of
its own, so a tick fires only while the loop passes. The step registers one repeating timer
entry each time the consumer starts, which celery does again after every broker reconnect
(the loop clears its timer entries when the connection is lost). A prefork child that hangs
on a job is the pool's to kill, by the task time limit; on a green pool (``-P gevent`` or
``-P eventlet``) the timer runs on its own green thread, so there a tick proves the process
and that thread rather than the loop.

The reporter and the job budget live on the app the worker runs (``HostedCelery.worker_progress``),
which celery hands every consumer step as its parent's ``app``: a config reload re-imports this
module while the worker runs, and the step and the tick still report through what the worker's
build registered.

A tick that raises (a withheld report, an unwritable liveness file, or no reporter registered)
is logged at ERROR by celery's hub and the entry repeats; the file then stops being refreshed,
which is what the probe reads.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, cast

from celery import Celery, bootsteps
from celery.worker.consumer.connection import Connection
from tai42_contract.backend.runtime import ProgressReporter
from tai42_kit.fork_gate import fork_gate

from tai42_backend_celery.core import embedded_beat, job_slots

if TYPE_CHECKING:
    from tai42_backend_celery.core.app import HostedCelery


@dataclass(frozen=True)
class _Pool:
    """How a celery pool runs its jobs, under the name the docs use."""

    name: str
    # Its jobs run in the worker process itself, not in forked children.
    in_process: bool


# Every pool celery ships, keyed by the module of its class: celery names its pools by module
# too, and the green pools are named without importing their libraries.
_POOLS: dict[str, _Pool] = {
    "celery.concurrency.prefork": _Pool("prefork", in_process=False),
    "celery.concurrency.solo": _Pool("solo", in_process=True),
    "celery.concurrency.thread": _Pool("threads", in_process=True),
    "celery.concurrency.gevent": _Pool("gevent", in_process=True),
    "celery.concurrency.eventlet": _Pool("eventlet", in_process=True),
}


@dataclass(frozen=True)
class WorkerProgress:
    """How a worker reports its progress: its reporter, and the budget every job of an in-process pool is held to."""

    report: ProgressReporter
    task_timeout: float


def register(app: HostedCelery, report: ProgressReporter, *, task_timeout: float) -> None:
    """Report the progress of ``app``'s worker through ``report``, by adding :class:`ProgressStep` to ``app``.

    ``task_timeout`` is the job budget every job of an in-process pool is held to. Worker-scoped,
    called from the worker runtime's ``build``, never at import; both are set on ``app`` as its
    ``worker_progress``. A second call with the same reporter keeps it and takes the budget; one
    with another reporter raises ``RuntimeError``, because one worker reports through one reporter.
    """
    registered = app.worker_progress
    if registered is not None and registered.report != report:
        raise RuntimeError("the celery worker already reports its progress through another reporter")
    app.worker_progress = WorkerProgress(report=report, task_timeout=task_timeout)
    consumer_steps(app).add(ProgressStep)


def _registered_progress(app: HostedCelery) -> WorkerProgress:
    progress = app.worker_progress
    if progress is None:
        raise RuntimeError(
            "the celery worker's progress step ticked with no reporter registered: register() runs from "
            "the worker runtime's build"
        )
    return progress


def consumer_steps(app: Celery) -> set[type[bootsteps.Step]]:
    """The custom bootsteps ``app`` adds to every worker consumer's blueprint.

    celery declares ``steps = None`` on the class and sets a ``defaultdict(set)`` in ``__init__``.
    """
    return cast("dict[str, set[type[bootsteps.Step]]]", app.steps)["consumer"]


def tick(app: HostedCelery) -> None:
    """Report one pass of ``app``'s worker, promising the next within one tick, unless what it cannot end is stuck."""
    report = _registered_progress(app).report
    embedded_beat.check(app)
    job_slots.check_wedged(app)
    report(next_within=job_slots.PROGRESS_TICK_SECONDS)


def hub_as_engine_loop(app: HostedCelery) -> AbstractContextManager[None]:
    """Run the calling thread, the one that runs ``app``'s worker hub, as the fork gate's engine loop for the block.

    A wait on the fork gate on that thread holds up the hub's next pass, so it reports the
    worker's progress with :func:`tick` of ``app``, once per tick of the hub timer.
    """
    return fork_gate.engine_loop(tick=partial(tick, app), tick_every=job_slots.PROGRESS_TICK_SECONDS)


class ProgressStep(bootsteps.StartStopStep):
    """Reports one pass of the hub loop per tick of the consumer's timer; registered on every consumer start."""

    requires = (Connection,)

    def __init__(self, parent: Any, **kwargs: Any) -> None:
        """Start with no timer entry: :meth:`start` registers it."""
        self.tref: Any = None
        super().__init__(parent, **kwargs)

    def start(self, parent: Any) -> None:
        """Watch the pool's jobs, then register the repeating tick.

        Whether the jobs are green threads of one OS thread the pool says itself (celery's
        ``is_green``). The step reports through the progress registered on the consumer's app.
        A pool whose class is not one celery ships raises ``RuntimeError``: how it runs its jobs,
        and so how to report for them, is unknown; so does an app with no progress registered.
        """
        app: HostedCelery = parent.app
        progress = _registered_progress(app)
        module = type(parent.pool).__module__
        pool = _POOLS.get(module)
        if pool is None:
            raise RuntimeError(
                f"the celery worker's pool {type(parent.pool).__qualname__} from {module} is not one celery ships "
                f"({', '.join(entry.name for entry in _POOLS.values())}); its progress cannot be reported"
            )
        app_tick = partial(tick, app)
        job_slots.watch(
            app,
            pool_name=pool.name,
            in_process=pool.in_process,
            green=parent.pool.is_green,
            budget=progress.task_timeout,
            ticks_in_job=pool.name == "solo",
            tick=app_tick,
        )
        self.tref = parent.timer.call_repeatedly(job_slots.PROGRESS_TICK_SECONDS, app_tick)

    def stop(self, parent: Any) -> None:
        """Cancel the tick, so a restarted consumer registers exactly one."""
        if self.tref is not None:
            self.tref.cancel()
            self.tref = None

    shutdown = stop
