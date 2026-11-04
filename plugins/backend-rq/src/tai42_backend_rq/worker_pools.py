"""The rq worker classes, one per pool, and what they share.

``prefork`` (:class:`CustomRQWorker`) forks a monitored work-horse per job. ``solo``
(:class:`CustomRQSimpleWorker`) and ``gevent`` (:class:`CustomRQGeventWorker`) run each job in
this process, beside a refresher on an OS thread that reports the worker's progress and refreshes
rq's heartbeats while the job runs; each keeps the job to its budget, and raises a stop into it,
with its own death penalty (:mod:`tai42_backend_rq.death_penalties`).
Every class reports the work loop's progress, also while a fork or a job waits on the work
loop's own thread for a config reload's re-import, and installs the kit's fork safety over every fork of the process
(:mod:`tai42_backend_rq.worker` describes the gate's invariant). Each class declares the job class
its jobs are dequeued as, which decides the event loop a job's coroutine runs on
(:mod:`tai42_backend_rq.job_loop`). The ``worker`` runtime builds the class its pool selects.
"""

from __future__ import annotations

import logging
import os
import signal
import time
from _thread import LockType
from collections.abc import Callable
from typing import Any, ClassVar

from rq import SimpleWorker, Worker
from rq.defaults import DEFAULT_LOGGING_DATE_FORMAT, DEFAULT_LOGGING_FORMAT
from rq.exceptions import StopRequested
from rq.timeouts import (
    HorseMonitorTimeoutException,
    TimerDeathPenalty,
    UnixSignalDeathPenalty,
)
from rq.worker import WorkerStatus
from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import ProgressReporter
from tai42_kit.backend import fork_safety
from tai42_kit.backend.job_context import running_job
from tai42_kit.fork_gate import JOB_SPAN_WAIT_SECONDS, fork_gate

from tai42_backend_rq.death_penalties import WatchedDeathPenalty, WatchedGeventDeathPenalty, WatchedTimerDeathPenalty
from tai42_backend_rq.job_loop import HostLoopJob, PrivateLoopJob
from tai42_backend_rq.scheduler_thread import ThreadedScheduler
from tai42_backend_rq.settings import rq_settings
from tai42_backend_rq.unpatched import original, os_thread_lock

logger = logging.getLogger(__name__)


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
# waiting on the fork gate on the work loop's thread reports on the same tick.
_REFRESH_TICK_SECONDS = 1.0


class _TaiWorkerMixin:
    """Worker behavior shared by both worker classes.

    * Skips rq's signal-handler install on every pool: the host owns the process's signals,
      and rq's handlers would replace the host's; shutdown arrives via
      :meth:`~tai42_backend_rq.worker.RqWorkerRuntime.request_drain`.
    * Bounds each idle dequeue poll (``_DEQUEUE_POLL_SECONDS``) AND returns to the
      outer ``work`` loop each poll so an off-thread drain request is honored
      within seconds even while the worker is idle.
    * Runs every job under the worker's current job budget (:meth:`prepare_execution`), marked
      as a backend job (:meth:`perform_job`).
    * Installs the kit's fork safety (:meth:`guard_forks`) over every fork of the process that
      runs the interpreter's fork hooks: every such child resets its inherited copy of the fork
      gate and evicts the monitoring writer it inherited. The fork gate's guard holds a job span
      around each fork, so on ``prefork`` and ``solo`` the ``prefork`` work-horse, the
      ``rq-scheduler`` the work loop spawns and its maintenance pass re-spawns, and any other fork
      made off an event loop's thread wait out a config reload's re-import of the manifest
      modules. A forked child is exposed only at the fork instant: it copies ``importlib``'s
      module-lock table at ``fork()``. The ``gevent`` pool runs rq's scheduler as a thread of its
      own process (:class:`CustomRQGeventWorker`), so no fork of its own waits. The non-forking
      pools also hold a span around each whole in-process job
      (:meth:`CustomRQSimpleWorker.execute_job`); a fork inside it nests in it.
    * Reports the work loop's progress (``report_progress``) on every rq heartbeat: each
      bounded dequeue poll and each job boundary, and once per ``_REFRESH_TICK_SECONDS`` while a
      fork or a job waits on the fork gate on the work loop's own thread (:meth:`work` runs the
      loop as the fork gate's engine loop): that wait holds up the loop's next pass. A fork
      waiting on any other thread reports nothing: the loop passes or stalls without it. A
      forked work-horse never reports: the liveness
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
        """Report a pass of the work loop while a fork or a job on the loop's thread waits on the fork gate."""
        self._report(next_within=_REFRESH_TICK_SECONDS)

    def work(self, *args: Any, **kwargs: Any) -> bool:
        """Run rq's work loop as the fork gate's engine loop: a wait on the gate on its thread reports its progress.

        Such a wait (the fork of a work-horse or of the ``rq-scheduler``, an in-process job's
        span) reports a pass each ``_REFRESH_TICK_SECONDS``. The forked work-horse never runs
        this loop.
        """
        with fork_gate.engine_loop(tick=self._tick_gate_wait, tick_every=_REFRESH_TICK_SECONDS):
            return super().work(*args, **kwargs)  # pyright: ignore[reportAttributeAccessIssue]

    def guard_forks(self) -> None:
        """Install the kit's fork safety over every fork of this process (see the class docstring).

        A fork that waits on a re-import waits for up to ``JOB_SPAN_WAIT_SECONDS``, reporting this
        worker's progress only when it waits on the work loop's thread (:meth:`work`).
        """
        fork_safety.guard_forks(fork_gate, timeout=JOB_SPAN_WAIT_SECONDS)

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
        """Install none of rq's signal handlers: the host owns the process's signals (see the class docstring)."""
        logger.info(
            "worker %r: the host owns the process's signals; shutdown is driven by warm-shutdown requests",
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
        blocked dequeue — a signal handler rq installed. Here the host owns the signals, and
        :meth:`~tai42_backend_rq.worker.RqWorkerRuntime.request_drain`, called from the host's
        loop, can only set ``_stop_requested``; it cannot raise into this blocked call. And rq's
        ``dequeue_job_and_maintain_ttl`` re-loops INTERNALLY on every ``DequeueTimeout`` when
        ``max_idle_time`` is ``None`` (a continuous worker), so control never returns to the
        outer ``work`` loop that checks the flag — an idle worker would then never stop.

        Bound each blocking poll (via ``max_idle_time = dequeue_timeout``) so the base returns
        every ``_DEQUEUE_POLL_SECONDS`` with no job, and re-check ``_stop_requested`` between
        polls: when set, raise ``StopRequested`` so the outer ``work`` loop breaks and the
        process exits cleanly (the recycle self-exit + any graceful backend shutdown). A
        dequeued job returns immediately, as rq's own call returns it.

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

    The fork guard spans the ``os.fork()`` INSTANT of each horse, not the job: the
    horse is a separate process, and what it inherits is a SNAPSHOT of ``importlib``'s
    module locks taken at the fork. A horse forked while no re-import is running therefore
    holds unlocked copies and is immune to every later parent re-import, however long its
    job runs. Holding the gate for the whole fork→reap span would block reloads behind
    arbitrarily long jobs for no added protection.

    The horse runs each job's coroutine on a fresh loop of its own (:class:`PrivateLoopJob`).
    """

    job_class = PrivateLoopJob

    def __init__(self, queues: Any, *args: Any, report_progress: ProgressReporter, **kwargs: Any) -> None:
        """Build the prefork worker and clear the killed-horse pid tracker."""
        super().__init__(queues, *args, report_progress=report_progress, **kwargs)
        # The horse pid ``kill_horse`` last targeted; the post-kill reap must not
        # self-timeout (see ``wait_for_horse``).
        self._killed_horse_pid = 0

    def fork_work_horse(self, job: Any, queue: Any) -> None:
        """Fork the work-horse, clearing the killed-horse pid tracker first.

        The fork guard's span closes the moment ``os.fork()`` returns in the parent; the horse
        then runs its whole job outside the gate against its own inherited lock table.
        """
        # Each new horse starts un-killed, so an OS-reused pid is never mistaken
        # for the killed one.
        self._killed_horse_pid = 0
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


class CustomRQSimpleWorker(_TaiWorkerMixin, SimpleWorker):
    """The non-forking worker (``solo`` and ``gevent`` pools): jobs run in the worker process itself.

    Its fork gate spans the WHOLE job (see :meth:`execute_job`), besides, on ``solo``, the fork
    guard's span over the fork instant of the ``rq-scheduler`` child it spawns. A job has no child
    and no inherited lock snapshot here: its body executes in this very process and imports
    lazily against the same live ``sys.modules`` a reload is churning, so the exposure lasts
    as long as the job does.

    On ``solo`` each job runs its coroutine on a fresh loop of its own (:class:`PrivateLoopJob`).

    A stop (rq's stop-job command) is raised into the running job's thread of execution the way the
    pool's task timeout is, by the penalty that guards it, which raises whichever of the two comes
    first and not the other; a job that ends on the stop is marked ``stopped`` by rq.
    """

    job_class: type[PrivateLoopJob | HostLoopJob] = PrivateLoopJob
    death_penalty_class = WatchedTimerDeathPenalty

    # Per OS thread: the worker whose refresher runs on it, and that refresher's own Redis client.
    _refresher_thread: ClassVar[Any] = original("_thread", "_local")()

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the worker with its guards: one over rq's end of a job's execution, one over a job's run.

        rq's end of a job's execution and each refresh of it hold the first, so a refresh never
        touches an execution rq has ended. The end of a job's run and each stop of it hold the
        second, so a stop never acts on a run that has ended (:meth:`kill_horse`). Both are native
        locks: the refresher and, on ``solo``, rq's stop-job listener are other OS threads.
        """
        super().__init__(*args, **kwargs)
        self._execution_guard = os_thread_lock()
        self._run_guard = os_thread_lock()
        self._running_job_id: str | None = None

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

    def kill_horse(self, sig: signal.Signals = signal.SIGKILL) -> None:
        """Stop the job rq's stop-job command names (``sig`` is a work-horse's, and this worker has none).

        rq calls this once the command names the job this worker runs, and has marked the job as
        stopped (``_stopped_job_id``), so that the job's failure is marked ``stopped``. The stop is
        raised into the job's guarded section, if one runs and its budget has not raised into it,
        where the job ends on it unless it catches it (:class:`WatchedDeathPenalty`). Otherwise
        nothing is raised: a job whose section has ended, or whose budget has fired, runs on as it
        was, and its mark stays, so a job that then ends in rq's failure handling is marked
        ``stopped``. A job whose run has ended meanwhile is no longer this worker's to stop, so its
        mark is dropped, as the end of a run drops it (:meth:`_end_run`).
        """
        with self._run_guard:
            stopped = self._stopped_job_id
            if stopped is None:
                return
            if stopped != self._running_job_id:
                self._stopped_job_id = None
                return
            WatchedDeathPenalty.interrupt(stopped)

    def cleanup_execution(self, job: Any, pipeline: Any) -> None:
        """End the job's execution, never during a refresh of it (see :meth:`_maintain_heartbeats_until`)."""
        with self._execution_guard:
            super().cleanup_execution(job, pipeline)

    def execute_job(self, job: Any, queue: Any) -> None:
        """Run the job in this process, refreshing the heartbeats while it runs.

        The fork gate is held for the whole body: an in-process job's imports race a
        concurrent re-import for its entire duration, so nothing narrower is sound. The
        prefork pool deliberately does NOT route through here — only the fork instant of
        its horse is gated, by the fork guard's span (:meth:`guard_forks`).

        A non-forking worker has no horse monitor to refresh heartbeats during a
        job, so a refresher thread ticks them on the same
        ``job_monitoring_interval``, keeping both pools on one liveness contract. It is an OS
        thread even under ``--pool gevent``, so a job that holds the CPU cannot starve it. The
        same thread reports the worker's progress while the job runs: the job occupies
        the work loop, up to its budget (:meth:`prepare_execution`). The penalty does not
        end a job blocked where its exception cannot reach, or one that catches it, so a job
        still running past its budget holds the worker's only slot: the thread then stops,
        and the liveness probe ends the worker. A job that waits on the gate runs on the work
        loop's thread, so it reports the worker's progress each ``_REFRESH_TICK_SECONDS``
        meanwhile (:meth:`work`): the refresher has not started yet.
        """
        with fork_gate.job_span(timeout=JOB_SPAN_WAIT_SECONDS):
            self._run_job_inline(job, queue)

    def _run_job_inline(self, job: Any, queue: Any) -> None:
        """The in-process job body, run with the fork gate held, beside the refresher's OS thread."""
        self.prepare_execution(job)
        done = os_thread_lock()
        done.acquire()
        ended = os_thread_lock()
        ended.acquire()
        _start_os_thread(self._refresh_until, (job, done, ended))
        with self._run_guard:
            self._running_job_id = job.id
        try:
            self.perform_job(job, queue)
        finally:
            try:
                self._end_run(job)
            finally:
                done.release()
                ended.acquire(timeout=self.job_monitoring_interval)
        self.set_state(WorkerStatus.IDLE)

    def _end_run(self, job: Any) -> None:
        """End ``job``'s run in this worker: no record of its sections and no mark of a stop of it remain.

        A stop that ended the job had its mark taken by rq's failure handling; a mark still left
        names a stop that did not end the job, and is dropped with the run. A raise that skipped
        a section's own end leaves that section's records, which end here
        (:meth:`WatchedDeathPenalty.end_run`).
        """
        with self._run_guard:
            self._running_job_id = None
            if self._stopped_job_id == job.id:
                self._stopped_job_id = None
            WatchedDeathPenalty.end_run(job.id)

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

        Both stop once a section of the job the penalty guards (its body, or a callback) is still
        running a tick past its timeout, logged once at ERROR: the job has not ended on the
        penalty's exception, so it holds the worker's only slot, and the liveness probe ends the
        worker. A failed refresh is logged at
        ERROR and retried on the next tick, never swallowed: a stopped heartbeat drops this worker
        from the census. Once rq has ended the job's execution (its result is stored, its callbacks
        may still run), there is no execution left to refresh, so the ticks only report. A failed
        report is the work loop's (:meth:`_report`).
        """
        last_refresh = time.monotonic()
        while not done.acquire(timeout=_REFRESH_TICK_SECONDS):
            timeout = WatchedDeathPenalty.overdue(job.id, margin=_REFRESH_TICK_SECONDS)
            if timeout is not None:
                logger.error(
                    "worker %r: job %s is still running past its %gs timeout and has not ended; "
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
    """The ``gevent`` pool's worker: the non-forking worker, its jobs run on the host's loop.

    Each job hands its coroutine to the host's running loop (:class:`HostLoopJob`), and each job's
    budget is kept by a timer on the job's hub, which ends the job's wait and cancels its coroutine
    there.
    rq's scheduler runs as a thread of this process (:class:`ThreadedScheduler`), a greenlet under
    the pool's patch, so a job that holds the CPU stalls it together with the host loop. This worker
    forks no child of its own. A thread ``threading`` starts in the patched process is a greenlet
    on the OS thread that runs the host's loop, so the fork guard, which never blocks a loop, makes
    no fork from one wait. A native OS thread runs no loop: the refresher each job starts
    (:func:`_start_os_thread`), and gevent's own threadpool. A fork from one waits out a running
    re-import and reports no progress; the refresher itself never forks. For every Python-level
    fork other code in the process makes, the guard resets the child's copy of the fork gate and
    evicts the monitoring writer the child inherited.
    """

    job_class = HostLoopJob
    death_penalty_class = WatchedGeventDeathPenalty

    def _start_scheduler(
        self,
        burst: bool = False,
        logging_level: str | None = "INFO",
        date_format: str = DEFAULT_LOGGING_DATE_FORMAT,
        log_format: str = DEFAULT_LOGGING_FORMAT,
    ) -> None:
        """Start rq's scheduler on a thread of this process once it holds a queue's scheduler lock.

        rq's own start forks the scheduler, which in this patched process would copy the host loop
        into the child (:mod:`tai42_backend_rq.scheduler_thread`). In burst mode it enqueues the
        jobs already due and starts nothing, as rq's does.
        """
        self.scheduler = ThreadedScheduler(
            self.queues,
            connection=self.connection,
            logging_level=logging_level if logging_level is not None else self.log.level,
            date_format=date_format,
            log_format=log_format,
            serializer=self.serializer,
        )
        self.scheduler.acquire_locks()
        if not self.scheduler.acquired_locks:
            return
        if burst:
            try:
                self.scheduler.register_birth()
                self.scheduler.enqueue_scheduled_jobs()
            finally:
                self.scheduler.release_locks()
                self.scheduler.register_death()
        else:
            self.scheduler.start()

    def stop_scheduler(self) -> None:
        """Stop the scheduler thread by its flag and wait for it to end.

        A live thread sees the flag within one scheduler interval, then releases its locks and deletes
        its key. A thread that has already died ends the wait at once and leaves its locks and key in
        Redis until their TTL (the scheduler interval plus 60 seconds) lets them expire. rq's own stop
        signals a scheduler process by pid, which a thread has none of. With no scheduler built there
        is nothing to stop.
        """
        scheduler = self.scheduler
        if scheduler is None:
            return
        scheduler.request_stop()
        if scheduler._process is not None:
            scheduler._process.join()


def _start_os_thread(target: Callable[..., None], args: tuple[Any, ...]) -> None:
    """Start ``target(*args)`` on a new OS thread, even in a process ``--pool gevent`` has patched.

    The patch turns ``threading``'s threads into greenlets, which a job that holds the CPU never
    lets run; gevent keeps the unpatched start function for exactly this.
    """
    original("_thread", "start_new_thread")(target, args)


def _client_like(connection: Any) -> Any:
    """A new Redis client of ``connection``'s class, on a new pool with the same connection options."""
    pool = connection.connection_pool
    return type(connection)(
        connection_pool=type(pool)(
            connection_class=pool.connection_class, max_connections=pool.max_connections, **pool.connection_kwargs
        )
    )
