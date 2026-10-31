"""The rq worker classes, one per pool, and what they share.

``prefork`` (:class:`CustomRQWorker`) forks a monitored work-horse per job. ``solo``
(:class:`CustomRQSimpleWorker`) and ``gevent`` (:class:`CustomRQGeventWorker`) run each job in
this process, beside a refresher on an OS thread that reports the worker's progress and refreshes
rq's heartbeats while the job runs; each keeps the job to its budget with its own death penalty.
Every class reports the work loop's progress, also while a job waits for a config reload's
re-import, and installs the fork gate's guard over every fork of the process
(:mod:`tai42_backend_rq.worker` describes the gate's invariant). The ``worker`` runtime builds
the class its pool selects.
"""

from __future__ import annotations

import importlib
import logging
import os
import signal
import sys
import threading
import time
from _thread import LockType
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, ClassVar

from rq import SimpleWorker, Worker
from rq.exceptions import StopRequested
from rq.timeouts import (
    BaseDeathPenalty,
    HorseMonitorTimeoutException,
    JobTimeoutException,
    TimerDeathPenalty,
    UnixSignalDeathPenalty,
)
from rq.worker import WorkerStatus
from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import ProgressReporter
from tai42_kit.backend.job_context import running_job
from tai42_kit.fork_gate import JOB_SPAN_WAIT_SECONDS, fork_gate

from tai42_backend_rq.settings import rq_settings

logger = logging.getLogger(__name__)


def _original(module: str, name: str) -> Any:
    """``module.name`` as it was before ``--pool gevent`` patched the process (unchanged when it has not).

    Only gevent's ``monkey`` module patches, so while it is not loaded nothing is patched and the
    attribute is read as it stands: the pools that never load gevent do not load it here either.
    """
    monkey = sys.modules.get("gevent.monkey")
    if monkey is None:
        return getattr(importlib.import_module(module), name)
    return monkey.get_original(module, name)


# Upper bound on one idle dequeue block, kept short so a warm-shutdown request
# (honored when the work loop next checks its stop flag) is not delayed by rq's
# default ``worker_ttl - 15`` poll off the main thread.
_DEQUEUE_POLL_SECONDS = 5

# How often the prefork parent re-checks whether its work-horse has exited, short
# enough to wake promptly and hit the ``job_monitoring_interval`` deadline in one tick.
_HORSE_POLL_SECONDS = 0.25

# How often the non-forking pools' refresher thread wakes while a job runs in-process: each wake
# reports the worker's progress, promising the next wake within this tick, and rq's own
# heartbeats are refreshed once per ``job_monitoring_interval`` of those wakes. A spawn or job
# waiting on the fork gate reports on the same tick.
_REFRESH_TICK_SECONDS = 1.0


class _TaiWorkerMixin:
    """Worker behavior shared by both worker classes.

    * Skips rq's signal-handler install when the work loop runs off the main
      thread (``signal.signal`` requires the main thread; shutdown then arrives
      via :meth:`~tai42_backend_rq.worker.RqWorkerRuntime.request_drain`).
    * Bounds each idle dequeue poll (``_DEQUEUE_POLL_SECONDS``) AND returns to the
      outer ``work`` loop each poll so an off-thread drain request is honored
      within seconds even while the worker is idle.
    * Runs every job under the worker's current job budget (:meth:`prepare_execution`), marked
      as a backend job (:meth:`perform_job`).
    * Holds a fork gate job span around each fork this worker makes — the work-horse
      (:meth:`CustomRQWorker.fork_work_horse`), the ``rq-scheduler`` spawned with the work loop
      (:meth:`_start_scheduler`) and re-spawned from its maintenance pass
      (:meth:`run_maintenance_tasks`) — so each waits out a config reload's re-import of the
      manifest modules where a stop request raised meanwhile reaches the work loop and
      nothing is forked. A forked child is exposed only at the fork instant: it copies
      ``importlib``'s module-lock table at ``fork()``. The fork gate's guard
      (:meth:`guard_forks`) covers every other fork of the process; a fork inside one of
      these spans nests in it. The non-forking pools also hold a span around each whole
      in-process job (:meth:`CustomRQSimpleWorker.execute_job`).
    * Reports the work loop's progress (``report_progress``) on every rq heartbeat: each
      bounded dequeue poll and each job boundary, and once per ``_REFRESH_TICK_SECONDS`` while a
      fork or a job waits on the fork gate. A forked work-horse never reports: the liveness
      file is the parent's signal, and the parent reports from its horse monitor
      (:meth:`CustomRQWorker.wait_for_horse`) while the horse runs.
    * A report that raises is kept, and the first one is logged at ERROR: the job in hand (and
      a forked horse) runs to its end under rq's monitor, the work loop then stops before its
      next dequeue, and :meth:`raise_for_liveness_failure` raises it once ``work`` has returned.
      rq's own loop catches every exception and returns normally, so a raise inside it would
      end the loop with no failure and leave a running horse unmonitored.
    """

    name: str  # provided by the RQ worker base class
    _stop_requested: bool  # provided by the RQ worker base class
    _report_progress: ProgressReporter
    _is_work_horse: bool
    _liveness_failure: Exception | None = None

    def __init__(self, *args: Any, report_progress: ProgressReporter, **kwargs: Any) -> None:
        """Build the rq worker, reporting each pass of its work loop to ``report_progress``."""
        super().__init__(*args, **kwargs)
        self._report_progress = report_progress
        self._is_work_horse = False

    def heartbeat(self, timeout: int | None = None, pipeline: Any = None) -> Any:
        """Report the work loop's progress, then refresh rq's own heartbeat.

        The report comes first, so a Redis call blocked in rq's refresh cannot hide a pass the
        loop has made. The forked work-horse skips it (see the class docstring).
        """
        if not self._is_work_horse:
            # The next heartbeat comes from the next bounded dequeue poll, or sooner from a job's monitor.
            self._report(next_within=_DEQUEUE_POLL_SECONDS)
        return super().heartbeat(timeout, pipeline)  # pyright: ignore[reportAttributeAccessIssue]

    def _report(self, *, next_within: float) -> None:
        """Report one pass of the work loop; the first report that raises is logged and kept for the loop's exit.

        A later failure is not logged again: the loop already stops before its next job, and
        :meth:`raise_for_liveness_failure` raises the first one.
        """
        try:
            self._report_progress(next_within=next_within)
        except Exception as exc:
            if self._liveness_failure is not None:
                return
            self._liveness_failure = exc
            logger.error(
                "worker %r: reporting its progress failed; it stops before its next job", self.name, exc_info=True
            )

    def _tick_gate_wait(self) -> None:
        """Report a pass of the work loop while a fork or a job waits on the fork gate."""
        self._report(next_within=_REFRESH_TICK_SECONDS)

    def guard_forks(self) -> None:
        """Guard every fork of this process with the fork gate, reporting this worker's progress while one waits."""
        fork_gate.guard_forks(
            timeout=JOB_SPAN_WAIT_SECONDS, tick=self._tick_gate_wait, tick_every=_REFRESH_TICK_SECONDS
        )

    def _fork_span(self) -> AbstractContextManager[None]:
        """The job span a fork this worker makes holds, reporting the worker's progress while it waits.

        Taken here rather than left to the fork guard's ``before`` hook: CPython reports an
        exception raised in that hook and forks anyway, while here a stop request raised
        during the wait propagates to the work loop.
        """
        return fork_gate.job_span(
            timeout=JOB_SPAN_WAIT_SECONDS, tick=self._tick_gate_wait, tick_every=_REFRESH_TICK_SECONDS
        )

    def run_maintenance_tasks(self) -> None:
        """Run rq's periodic maintenance inside a fork span.

        It re-spawns the scheduler process when that process has died. The span covers the
        whole body, which spawns at most once and otherwise does bounded Redis maintenance
        (registry cleaning).
        """
        with self._fork_span():
            super().run_maintenance_tasks()  # pyright: ignore[reportAttributeAccessIssue]

    def _start_scheduler(self, *args: Any, **kwargs: Any) -> None:
        """Start the scheduler process ``work()`` spawns before its dequeue loop, inside a fork span.

        The span covers the whole body, which spawns at most once and otherwise does bounded
        Redis work (scheduler construction plus ``acquire_locks``; in burst mode it enqueues
        due jobs and forks nothing).
        """
        with self._fork_span():
            super()._start_scheduler(*args, **kwargs)  # pyright: ignore[reportAttributeAccessIssue]

    def raise_for_liveness_failure(self) -> None:
        """Raise the first failed progress report, after the work loop has returned."""
        if self._liveness_failure is not None:
            raise RuntimeError(
                f"rq worker {self.name!r} stopped because reporting its progress failed"
            ) from self._liveness_failure

    # The timer-based penalty is thread-agnostic, so it is what the PARENT arms
    # (``signal.signal`` cannot arm the signal penalty off the main thread). Its
    # async exception cannot interrupt a C-blocked thread, so the horse monitor
    # does not rely on it (``wait_for_horse`` self-times-out) and the work-horse
    # child restores the signal penalty (``main_work_horse``), where SIGALRM works.
    death_penalty_class = TimerDeathPenalty

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is threading.main_thread():
            super()._install_signal_handlers()  # pyright: ignore[reportAttributeAccessIssue]
            return
        logger.info(
            "worker %r runs off the main thread; shutdown is driven by warm-shutdown requests, not signal handlers",
            self.name,
        )

    @property
    def dequeue_timeout(self) -> int:
        return _DEQUEUE_POLL_SECONDS

    def prepare_execution(self, job: Any) -> Any:
        """Run ``job`` under this worker's job budget, ``RQ_TASK_TIMEOUT``.

        A job carries the timeout it was stored with — the enqueuing process's budget, and for a
        recurring schedule the budget of its first fire, kept for every later fire. rq's worker
        reads ``job.timeout`` for the horse monitor and the death penalty, so it is set here, where
        both worker classes prepare every job before running it (the forked horse inherits it).
        """
        job.timeout = rq_settings().task_timeout
        return super().prepare_execution(job)  # pyright: ignore[reportAttributeAccessIssue]

    def perform_job(self, job: Any, queue: Any) -> bool:
        """Perform ``job`` marked as a backend job (:func:`~tai42_kit.backend.job_context.running_job`).

        Every pool performs its jobs here, in the process that runs them: the work-horse child
        on the prefork pool, after its fork, and the worker itself on the others. A tool the
        job runs that would wait for another job's result refuses.
        """
        with running_job():
            return super().perform_job(job, queue)  # pyright: ignore[reportAttributeAccessIssue]

    def dequeue_job_and_maintain_ttl(self, timeout: int | None, max_idle_time: int | None = None) -> Any:
        """Honor an off-thread drain request even while IDLE.

        rq's own idle shutdown relies on ``request_stop`` raising ``StopRequested`` INSIDE the
        blocked dequeue — a signal delivered on the worker's own (main) thread. Here the work
        loop runs off the main thread, so :meth:`~tai42_backend_rq.worker.RqWorkerRuntime.request_drain` can only set
        ``_stop_requested``; it cannot raise into this blocked call. And rq's
        ``dequeue_job_and_maintain_ttl`` re-loops INTERNALLY on every ``DequeueTimeout`` when
        ``max_idle_time`` is ``None`` (a continuous worker), so control never returns to the
        outer ``work`` loop that checks the flag — an idle worker would then never stop.

        Bound each blocking poll (via ``max_idle_time = dequeue_timeout``) so the base returns
        every ``_DEQUEUE_POLL_SECONDS`` with no job, and re-check ``_stop_requested`` between
        polls: when set, raise ``StopRequested`` so the outer ``work`` loop breaks and the
        process exits cleanly (the recycle self-exit + any graceful backend shutdown). A
        dequeued job returns immediately, exactly as before.

        A failed progress report stops the loop the same way, before it takes another job.
        """
        poll = self.dequeue_timeout
        while True:
            if self._stop_requested or self._liveness_failure is not None:
                raise StopRequested()
            result = super().dequeue_job_and_maintain_ttl(poll, max_idle_time=poll)  # pyright: ignore[reportAttributeAccessIssue]
            if result is not None:
                return result


class CustomRQWorker(_TaiWorkerMixin, Worker):
    """The prefork worker: RQ's native forking ``execute_job``, a monitored work-horse child per job.

    The fork gate spans the ``os.fork()`` INSTANT of each horse, not the job: the
    horse is a separate process, and what it inherits is a SNAPSHOT of ``importlib``'s
    module locks taken at the fork. A horse forked while no re-import is running therefore
    holds unlocked copies and is immune to every later parent re-import, however long its
    job runs. Holding the gate for the whole fork→reap span would block reloads behind
    arbitrarily long jobs for no added protection.
    """

    def __init__(self, queues: Any, *args: Any, report_progress: ProgressReporter, **kwargs: Any) -> None:
        """Build the prefork worker and clear the killed-horse pid tracker."""
        super().__init__(queues, *args, report_progress=report_progress, **kwargs)
        # The horse pid ``kill_horse`` last targeted; the post-kill reap must not
        # self-timeout (see ``wait_for_horse``).
        self._killed_horse_pid = 0

    def fork_work_horse(self, job: Any, queue: Any) -> None:
        """Fork the work-horse inside a fork span, clearing the killed-horse pid tracker first.

        ``super()`` returns in the parent as soon as ``os.fork()`` has returned, so the span
        closes the moment the child exists; the horse then runs its whole job outside the
        gate against its own inherited lock table.
        """
        # Each new horse starts un-killed, so an OS-reused pid is never mistaken
        # for the killed one.
        self._killed_horse_pid = 0
        with self._fork_span():
            super().fork_work_horse(job, queue)

    def kill_horse(self, sig: signal.Signals = signal.SIGKILL) -> None:
        """Record the horse pid being killed, then signal it (default SIGKILL)."""
        self._killed_horse_pid = self.horse_pid
        super().kill_horse(sig)

    def wait_for_horse(self) -> tuple[int | None, int | None, Any | None]:
        """Reap the work-horse by POLLING rather than blocking, so the monitor loop keeps ticking while a job runs.

        RQ's monitor wraps this in a death penalty sized to
        ``job_monitoring_interval`` that refreshes heartbeats and enforces the
        runaway backstop; off the main thread its timer exception is never
        delivered to a thread blocked in ``os.wait4``, so a blocking reap would
        silence the monitor for the whole job. Polling with ``WNOHANG`` and raising
        ``HorseMonitorTimeoutException`` one tick short of the interval keeps the
        loop cycling. The post-``kill_horse`` reap runs outside any penalty context,
        so once the horse is killed this polls until reaped and never self-times-out.

        Each poll tick reports the parent's progress: while a horse runs, this loop is the
        parent's work loop.
        """
        reaping_killed_horse = self._killed_horse_pid == self.horse_pid
        deadline = time.monotonic() + max(self.job_monitoring_interval - _HORSE_POLL_SECONDS, _HORSE_POLL_SECONDS)
        while True:
            self._report(next_within=_HORSE_POLL_SECONDS)
            try:
                pid, stat, rusage = os.wait4(self.horse_pid, os.WNOHANG)
            except ChildProcessError:
                # Already reaped (rq's own semantics for a vanished horse).
                return None, None, None
            if pid:
                return pid, stat, rusage
            if not reaping_killed_horse and time.monotonic() >= deadline:
                raise HorseMonitorTimeoutException
            time.sleep(_HORSE_POLL_SECONDS)

    def main_work_horse(self, job: Any, queue: Any) -> None:
        """Run the forked child's work loop under rq's SIGNAL death penalty.

        The forking thread becomes the child's main thread, so ``signal.signal`` works here
        (and only SIGALRM can interrupt C-blocked job code). The horse stops reporting
        progress: the liveness file belongs to the parent.
        """
        self._is_work_horse = True
        self.death_penalty_class = UnixSignalDeathPenalty
        super().main_work_horse(job, queue)

    def perform_job(self, job: Any, queue: Any) -> bool:
        """Run one job in the work-horse child, flushing monitoring before exit.

        The work-horse leaves via ``os._exit``, which skips atexit, so buffered
        spans must be flushed explicitly. A failed flush is logged at ERROR (not
        raised, which would turn a finished job into a horse failure).
        """
        try:
            return super().perform_job(job, queue)
        finally:
            try:
                tai42_app.monitoring.active.writer.flush()
            except Exception:
                logger.error(
                    "work-horse %s: monitoring flush failed; buffered spans are lost", os.getpid(), exc_info=True
                )


class _WatchedDeathPenalty(BaseDeathPenalty):
    """A job-budget penalty that records each section it guards while the section runs.

    rq guards a job's body, and each of its callbacks, with one penalty named by the job's id. The
    refresher of a non-forking worker reads the record (:meth:`overdue`) to tell a section the
    penalty has not ended from one it has: the penalty's exception reaches the job within moments
    wherever it can, so a section still running a tick past its deadline is blocked where the
    exception cannot reach.
    """

    # Job id -> (monotonic deadline, timeout) of the section its penalty guards now.
    _armed: ClassVar[dict[str, tuple[float, float]]] = {}

    def __init__(
        self, timeout: float, exception: type[JobTimeoutException] = JobTimeoutException, **kwargs: Any
    ) -> None:
        super().__init__(timeout, exception, **kwargs)
        self._job_id: str | None = kwargs.get("job_id")

    def setup_death_penalty(self) -> None:
        super().setup_death_penalty()
        if self._job_id is not None and self._timeout > 0:
            self._armed[self._job_id] = (time.monotonic() + self._timeout, self._timeout)

    def cancel_death_penalty(self) -> None:
        if self._job_id is not None:
            self._armed.pop(self._job_id, None)
        super().cancel_death_penalty()

    @classmethod
    def overdue(cls, job_id: str, margin: float) -> float | None:
        """The timeout of ``job_id``'s guarded section when it still runs ``margin`` seconds past its deadline."""
        armed = cls._armed.get(job_id)
        if armed is None:
            return None
        deadline, timeout = armed
        return timeout if time.monotonic() > deadline + margin else None


class _WatchedTimerDeathPenalty(_WatchedDeathPenalty, TimerDeathPenalty):
    """The ``solo`` pool's penalty: rq's timer, which raises the job's timeout into the job's OS thread.

    The exception reaches the job at its next Python bytecode, so it cannot end a job blocked in C code.
    """


class _GeventDeathPenalty(BaseDeathPenalty):
    """A ``gevent.Timeout`` that raises the job's timeout into the job's greenlet.

    It fires when the job yields to the hub, so it cannot end a job that holds the CPU or blocks
    in C code without yielding.
    """

    def __init__(
        self, timeout: float, exception: type[JobTimeoutException] = JobTimeoutException, **kwargs: Any
    ) -> None:
        super().__init__(timeout, exception, **kwargs)
        self._gevent_timeout: Any = None

    def setup_death_penalty(self) -> None:
        if self._timeout <= 0:
            return
        from gevent import Timeout

        self._gevent_timeout = Timeout(
            self._timeout, self._exception(f"Task exceeded maximum timeout value ({self._timeout} seconds)")
        )
        self._gevent_timeout.start()

    def cancel_death_penalty(self) -> None:
        if self._gevent_timeout is not None:
            self._gevent_timeout.close()
            self._gevent_timeout = None


class _WatchedGeventDeathPenalty(_WatchedDeathPenalty, _GeventDeathPenalty):
    """The ``gevent`` pool's penalty.

    rq's timer cannot serve this pool: the patched process gives the timer a greenlet's identity
    to target, which is no thread, so the timer fails and the job runs on.
    """


class CustomRQSimpleWorker(_TaiWorkerMixin, SimpleWorker):
    """The non-forking worker (``solo`` and ``gevent`` pools): jobs run in the worker process itself.

    Its fork gate spans the WHOLE job (see :meth:`execute_job`), besides the fork guard's
    span over the fork instant of the ``rq-scheduler`` child it spawns. A job has no child
    and no inherited lock snapshot here: its body executes in this very process and imports
    lazily against the same live ``sys.modules`` a reload is churning, so the exposure lasts
    as long as the job does.
    """

    death_penalty_class = _WatchedTimerDeathPenalty

    # Per OS thread: the worker whose refresher runs on it, and that refresher's own Redis client.
    _refresher_thread: ClassVar[Any] = _original("_thread", "_local")()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the worker with the guard that rq's end of a job's execution and each refresh of it hold.

        A refresh then never touches an execution rq has ended. It is a native lock: the
        refresher is another OS thread.
        """
        super().__init__(*args, **kwargs)
        self._execution_guard = _os_thread_lock()

    @property
    def connection(self) -> Any:
        """The worker's Redis client; on this worker's refresher thread, that thread's own client.

        Under ``--pool gevent`` a client's sockets belong to the OS thread whose hub created them,
        so the refresher thread cannot share the work loop's client: it refreshes rq's heartbeats
        on a client of its own, built on first use and closed when it ends (:meth:`_refresh_until`).
        """
        refresher = self._refresher_thread
        if getattr(refresher, "worker", None) is not self:
            return self._work_loop_connection
        if refresher.client is None:
            refresher.client = _client_like(self._work_loop_connection)
        return refresher.client

    @connection.setter
    def connection(self, connection: Any) -> None:
        self._work_loop_connection = connection

    def cleanup_execution(self, job: Any, pipeline: Any) -> None:
        """End the job's execution, never during a refresh of it (see :meth:`_maintain_heartbeats_until`)."""
        with self._execution_guard:
            super().cleanup_execution(job, pipeline)

    def execute_job(self, job: Any, queue: Any) -> None:
        """Run the job in this process, refreshing the heartbeats while it runs.

        The fork gate is held for the whole body: an in-process job's imports race a
        concurrent re-import for its entire duration, so nothing narrower is sound. The
        prefork pool deliberately does NOT route through here — only the fork instant of
        its horse is gated, by the span :meth:`CustomRQWorker.fork_work_horse` holds.

        A non-forking worker has no horse monitor to refresh heartbeats during a
        job, so a refresher thread ticks them on the same
        ``job_monitoring_interval``, keeping both pools on one liveness contract. It is an OS
        thread even under ``--pool gevent``, so a job that holds the CPU cannot starve it. The
        same thread reports the worker's progress while the job runs: the job occupies
        the work loop, up to its budget (:meth:`prepare_execution`). The timer penalty
        cannot interrupt a job blocked in C code, so a job still running past its budget
        holds the worker's only slot: the thread then stops, and the liveness probe ends
        the worker. A job that waits on the gate reports the worker's progress each
        ``_REFRESH_TICK_SECONDS`` meanwhile: the refresher has not started yet.
        """
        with fork_gate.job_span(
            timeout=JOB_SPAN_WAIT_SECONDS, tick=self._tick_gate_wait, tick_every=_REFRESH_TICK_SECONDS
        ):
            self._run_job_inline(job, queue)

    def _run_job_inline(self, job: Any, queue: Any) -> None:
        """The in-process job body, run with the fork gate held, beside the refresher's OS thread."""
        self.prepare_execution(job)
        done = _os_thread_lock()
        done.acquire()
        ended = _os_thread_lock()
        ended.acquire()
        _start_os_thread(self._refresh_until, (job, done, ended))
        try:
            self.perform_job(job, queue)
        finally:
            done.release()
            ended.acquire(timeout=self.job_monitoring_interval)
        self.set_state(WorkerStatus.IDLE)

    def _refresh_until(self, job: Any, done: LockType, ended: LockType) -> None:
        """The refresher's OS thread: run :meth:`_maintain_heartbeats_until`, close its client, release ``ended``."""
        refresher = self._refresher_thread
        refresher.worker, refresher.client = self, None
        try:
            self._maintain_heartbeats_until(job, done)
        finally:
            client = refresher.client
            refresher.worker = refresher.client = None
            try:
                if client is not None:
                    client.close()
            finally:
                ended.release()

    def _maintain_heartbeats_until(self, job: Any, done: LockType) -> None:
        """Report progress every tick and refresh rq's heartbeats every monitoring interval, until ``done`` is released.

        Both stop once a section of the job the timer penalty guards (its body, or a callback) is
        still running a tick past its timeout, logged once at ERROR: the timer has not ended it, so
        it holds the worker's only slot and this pool cannot end it. A failed refresh is logged at
        ERROR and retried on the next tick, never swallowed: a stopped heartbeat drops this worker
        from the census. Once rq has ended the job's execution (its result is stored, its callbacks
        may still run), there is no execution left to refresh, so the ticks only report. A failed
        report is the work loop's (:meth:`_report`).
        """
        last_refresh = time.monotonic()
        while not done.acquire(timeout=_REFRESH_TICK_SECONDS):
            timeout = _WatchedDeathPenalty.overdue(job.id, margin=_REFRESH_TICK_SECONDS)
            if timeout is not None:
                logger.error(
                    "worker %r: job %s is still running past its %gs timeout, which this pool cannot end; "
                    "it reports no more progress, so the liveness probe ends this worker",
                    self.name,
                    job.id,
                    timeout,
                )
                return
            self._report(next_within=_REFRESH_TICK_SECONDS)
            if time.monotonic() - last_refresh < self.job_monitoring_interval:
                continue
            last_refresh = time.monotonic()
            try:
                with self._execution_guard:
                    if self.execution is not None:
                        self.maintain_heartbeats(job)
            except Exception:
                logger.error("worker %r: heartbeat refresh failed while running %s", self.name, job.id, exc_info=True)


class CustomRQGeventWorker(CustomRQSimpleWorker):
    """The ``gevent`` pool's worker: the non-forking worker, with each job's budget kept by a gevent timeout."""

    death_penalty_class = _WatchedGeventDeathPenalty


def _start_os_thread(target: Callable[..., None], args: tuple[Any, ...]) -> None:
    """Start ``target(*args)`` on a new OS thread, even in a process ``--pool gevent`` has patched.

    The patch turns ``threading``'s threads into greenlets, which a job that holds the CPU never
    lets run; gevent keeps the unpatched start function for exactly this.
    """
    _original("_thread", "start_new_thread")(target, args)


def _os_thread_lock() -> LockType:
    """A lock that blocks only the OS thread that waits on it, even in a process gevent has patched."""
    return _original("_thread", "allocate_lock")()


def _client_like(connection: Any) -> Any:
    """A new Redis client of ``connection``'s class, on a new pool with the same connection options."""
    pool = connection.connection_pool
    return type(connection)(
        connection_pool=type(pool)(
            connection_class=pool.connection_class, max_connections=pool.max_connections, **pool.connection_kwargs
        )
    )
