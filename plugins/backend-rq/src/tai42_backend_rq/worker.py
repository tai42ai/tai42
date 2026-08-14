"""The RQ worker runtime: worker classes, fork safety, and the ``worker``
launch entrypoint.

``launch(["worker", ...])`` drives :class:`RqWorkerRuntime`, which the shared
``ManagedBackend`` base runs on a dedicated daemon thread — readiness gating,
shutdown wiring and drain-on-cancellation are the base's, and this module binds
only how RQ starts, drains and recycles. The blocking ``worker.work()`` loop has
to stay OFF the process's event loop: the app's bus subscription lives there, so
a blocked loop would starve op delivery.

That same loop forks a work-horse per job, and the bus delivers config reloads which
pop this process's manifest modules out of ``sys.modules`` and re-import them. A horse
forked DURING that re-import inherits a held ``importlib`` per-module lock whose owner
thread does not exist post-fork, blocks forever on the first import it needs, and wedges
the parent in ``monitor_work_horse`` until the runaway backstop fires — the queue drains
nothing for the whole span.

``tai42_kit.fork_gate`` holds the invariant (canonical statement in its module
docstring): while a re-import runs, no child may be FORKED and no in-process job may
RUN. This worker takes the work-loop side per pool — prefork gates the ``fork()``
instant (a child forked outside the window inherits unlocked copies and is immune
thereafter), solo/gevent gate the whole in-process job — plus every scheduler spawn.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Self

import click
from redis import Redis
from rq import Queue, SimpleWorker, Worker
from rq.exceptions import StopRequested
from rq.timeouts import HorseMonitorTimeoutException, TimerDeathPenalty, UnixSignalDeathPenalty
from rq.worker import WorkerStatus
from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode
from tai42_kit.fork_gate import fork_gate

from tai42_backend_rq.settings import rq_settings

logger = logging.getLogger(__name__)

# Upper bound on one idle dequeue block, kept short so a warm-shutdown request
# (honored when the work loop next checks its stop flag) is not delayed by rq's
# default ``worker_ttl - 15`` poll off the main thread.
_DEQUEUE_POLL_SECONDS = 5

# How often the prefork parent re-checks whether its work-horse has exited, short
# enough to wake promptly and hit the ``job_monitoring_interval`` deadline in one tick.
_HORSE_POLL_SECONDS = 0.25

# Upper bound on the wait for a config reload's module re-import to finish before this
# worker spawns a child (or, on the non-forking pools, starts a job in-process). Sized
# generously against the REBUILD it waits on — seconds, not minutes — rather than
# against the job: the job is already dequeued, rq re-delivers nothing here, so this
# wait is the only thing holding it and a queued job must never be dropped because a
# reload was slow. A reload that somehow overruns even this is reported by the fork
# gate's ERROR and the spawn proceeds unguarded.
_FORK_GATE_WAIT_SECONDS = 120


class _TaiWorkerMixin:
    """Worker behavior shared by both worker classes.

    * Skips rq's signal-handler install when the work loop runs off the main
      thread (``signal.signal`` requires the main thread; shutdown then arrives
      via :meth:`RqWorkerRuntime.request_drain`).
    * Bounds each idle dequeue poll (``_DEQUEUE_POLL_SECONDS``) AND returns to the
      outer ``work`` loop each poll so an off-thread drain request is honored
      within seconds even while the worker is idle.
    * Holds a :meth:`fork_gate.job_span` around the two SCHEDULER-SPAWNING bodies — the
      start rq performs with the work loop and the re-spawn from its maintenance pass —
      so no child is created while this process re-imports its manifest modules on a
      config reload. Each span covers its whole body: at most one spawn plus bounded
      Redis work. (Only :meth:`CustomRQWorker.fork_work_horse` is scoped to the fork
      instant itself, which is all a forked child is exposed to — it copies
      ``importlib``'s module-lock table at ``fork()``.) The per-job spans differ by pool
      and live on the two worker classes (see each).
    """

    name: str  # provided by the RQ worker base class
    _stop_requested: bool  # provided by the RQ worker base class

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

    def dequeue_job_and_maintain_ttl(self, timeout: int | None, max_idle_time: int | None = None) -> Any:
        """Honor an off-thread drain request even while IDLE.

        rq's own idle shutdown relies on ``request_stop`` raising ``StopRequested`` INSIDE the
        blocked dequeue — a signal delivered on the worker's own (main) thread. Here the work
        loop runs off the main thread, so :meth:`RqWorkerRuntime.request_drain` can only set
        ``_stop_requested``; it cannot raise into this blocked call. And rq's
        ``dequeue_job_and_maintain_ttl`` re-loops INTERNALLY on every ``DequeueTimeout`` when
        ``max_idle_time`` is ``None`` (a continuous worker), so control never returns to the
        outer ``work`` loop that checks the flag — an idle worker would then never stop.

        Bound each blocking poll (via ``max_idle_time = dequeue_timeout``) so the base returns
        every ``_DEQUEUE_POLL_SECONDS`` with no job, and re-check ``_stop_requested`` between
        polls: when set, raise ``StopRequested`` so the outer ``work`` loop breaks and the
        process exits cleanly (the recycle self-exit + any graceful backend shutdown). A
        dequeued job returns immediately, exactly as before."""
        poll = self.dequeue_timeout
        while True:
            if self._stop_requested:
                raise StopRequested()
            result = super().dequeue_job_and_maintain_ttl(poll, max_idle_time=poll)  # pyright: ignore[reportAttributeAccessIssue]
            if result is not None:
                return result

    def run_maintenance_tasks(self) -> None:
        """Run rq's periodic maintenance under the fork gate: it re-spawns the scheduler
        process when that process has died, which is the same hazard as a work-horse
        fork. The span covers the whole body, which spawns at most once and otherwise
        does bounded Redis maintenance (registry cleaning)."""
        with fork_gate.job_span(timeout=_FORK_GATE_WAIT_SECONDS):
            super().run_maintenance_tasks()  # pyright: ignore[reportAttributeAccessIssue]

    def _start_scheduler(self, *args: Any, **kwargs: Any) -> None:
        """Gate the work loop's FIRST scheduler spawn too.

        ``work()`` starts the scheduler process before entering its dequeue loop, and
        that start is a ``fork()`` like any other. It is rarer than the maintenance
        re-spawn (once per worker, at startup) but not impossible to race: a worker
        starting while a sibling-triggered reload is already re-importing in this
        process would spawn its scheduler straight into the window.

        The span covers the whole body, which spawns at most once and otherwise does
        bounded Redis work (scheduler construction plus ``acquire_locks``; in burst mode
        it enqueues due jobs and forks nothing at all).
        """
        with fork_gate.job_span(timeout=_FORK_GATE_WAIT_SECONDS):
            super()._start_scheduler(*args, **kwargs)  # pyright: ignore[reportAttributeAccessIssue]


class CustomRQWorker(_TaiWorkerMixin, Worker):
    """The prefork worker: RQ's native forking ``execute_job`` (a monitored
    work-horse child per job, with timeout enforcement).

    Its fork gate spans the ``os.fork()`` INSTANT (see :meth:`fork_work_horse`), not the
    job: the horse is a separate process, and what it inherits is a SNAPSHOT of
    ``importlib``'s module locks taken at the fork. A horse forked while no re-import is
    running therefore holds unlocked copies and is immune to every later parent
    re-import, however long its job runs. Holding the gate for the whole fork→reap span
    would block reloads behind arbitrarily long jobs for no added protection.
    """

    def __init__(self, queues: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(queues, *args, **kwargs)
        # The horse pid ``kill_horse`` last targeted; the post-kill reap must not
        # self-timeout (see ``wait_for_horse``).
        self._killed_horse_pid = 0

    def fork_work_horse(self, job: Any, queue: Any) -> None:
        """Fork the work-horse under the gate — THE seam that must not race a re-import.

        ``super()`` returns in the parent as soon as ``os.fork()`` has returned, so the
        span closes the moment the child exists; the horse then runs its whole job
        outside the gate against its own inherited lock table.
        """
        # Each new horse starts un-killed, so an OS-reused pid is never mistaken
        # for the killed one.
        self._killed_horse_pid = 0
        with fork_gate.job_span(timeout=_FORK_GATE_WAIT_SECONDS):
            super().fork_work_horse(job, queue)

    def kill_horse(self, sig: signal.Signals = signal.SIGKILL) -> None:
        self._killed_horse_pid = self.horse_pid
        super().kill_horse(sig)

    def wait_for_horse(self) -> tuple[int | None, int | None, Any | None]:
        """Reap the work-horse by POLLING rather than blocking, so the parent's
        monitor loop keeps ticking while a job runs.

        RQ's monitor wraps this in a death penalty sized to
        ``job_monitoring_interval`` that refreshes heartbeats and enforces the
        runaway backstop; off the main thread its timer exception is never
        delivered to a thread blocked in ``os.wait4``, so a blocking reap would
        silence the monitor for the whole job. Polling with ``WNOHANG`` and raising
        ``HorseMonitorTimeoutException`` one tick short of the interval keeps the
        loop cycling. The post-``kill_horse`` reap runs outside any penalty context,
        so once the horse is killed this polls until reaped and never self-times-out.
        """
        reaping_killed_horse = self._killed_horse_pid == self.horse_pid
        deadline = time.monotonic() + max(self.job_monitoring_interval - _HORSE_POLL_SECONDS, _HORSE_POLL_SECONDS)
        while True:
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
        """Run the forked child's work loop under rq's SIGNAL death penalty. The
        forking thread becomes the child's main thread, so ``signal.signal`` works
        here (and only SIGALRM can interrupt C-blocked job code)."""
        self.death_penalty_class = UnixSignalDeathPenalty
        super().main_work_horse(job, queue)

    def perform_job(self, job: Any, queue: Any) -> bool:
        """Run one job in the work-horse child, flushing monitoring before exit.

        The work-horse leaves via ``os._exit``, which skips atexit, so buffered
        spans must be flushed explicitly. A failed flush is logged at ERROR (not
        raised, which would turn a finished job into a horse failure)."""
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
    """The non-forking worker (``solo`` and ``gevent`` pools): jobs run in the
    worker process itself.

    Its fork gate spans the WHOLE job (see :meth:`execute_job`), unlike the prefork
    pool's fork-instant span. There is no child and no inherited lock snapshot here: the
    job body executes in this very process and imports lazily against the same live
    ``sys.modules`` a reload is churning, so the exposure lasts as long as the job does.
    """

    def execute_job(self, job: Any, queue: Any) -> None:
        """Run the job in this process, refreshing the heartbeats while it runs.

        The fork gate is held for the whole body: an in-process job's imports race a
        concurrent re-import for its entire duration, so nothing narrower is sound. The
        prefork pool deliberately does NOT route through here — it gates the fork
        instant instead (:meth:`CustomRQWorker.fork_work_horse`).

        A non-forking worker has no horse monitor to refresh heartbeats during a
        job, so a refresher thread ticks them on the same
        ``job_monitoring_interval``, keeping both pools on one liveness contract.
        """
        with fork_gate.job_span(timeout=_FORK_GATE_WAIT_SECONDS):
            self._run_job_inline(job, queue)

    def _run_job_inline(self, job: Any, queue: Any) -> None:
        """The in-process job body, run with the fork gate held."""
        self.prepare_execution(job)
        done = threading.Event()
        beat = threading.Thread(
            target=self._maintain_heartbeats_until, args=(job, done), name=f"rq-heartbeat-{job.id}", daemon=True
        )
        beat.start()
        try:
            self.perform_job(job, queue)
        finally:
            done.set()
            beat.join(timeout=self.job_monitoring_interval)
        self.set_state(WorkerStatus.IDLE)

    def _maintain_heartbeats_until(self, job: Any, done: threading.Event) -> None:
        """Refresh this worker's heartbeats every monitoring interval until the job
        finishes. A failed refresh is logged at ERROR and retried, never swallowed —
        a stopped heartbeat drops this worker from the census."""
        while not done.wait(self.job_monitoring_interval):
            try:
                self.maintain_heartbeats(job)
            except Exception:
                logger.error("worker %r: heartbeat refresh failed while running %s", self.name, job.id, exc_info=True)


def setup_gevent() -> None:
    """Monkey-patch blocking primitives so the gevent pool can overlap
    I/O-bound jobs inside the single non-forking worker process.

    ``patch_all`` runs before the fork gate's ``Condition`` is first used, so the waiter
    locks that ``Condition.wait`` allocates per wait are green ones: a gate wait yields
    to the hub instead of blocking it. It also patches ``threading.get_ident`` to return
    a per-GREENLET identity, so the gate's ``_owner`` becomes a greenlet identity — which
    is the correct granularity here, since a greenlet is what runs a rebuild body or a
    job under this pool.

    The Condition's OUTER lock is the plain ``threading.RLock`` created when
    ``tai42_kit.fork_gate`` was imported (before patching), so it stays a native RLock
    reentrant per OS THREAD. Every greenlet here shares one OS thread, so that lock would
    NOT exclude them from each other: a greenlet that yielded while holding it would let
    another greenlet re-enter the critical section and see half-updated counters. What
    makes the design safe is that the gate's ``_cond`` critical sections contain no yield
    points — ``Condition.wait`` releases the outer lock before parking, and the report
    strings are deliberately logged outside the lock. Do not add an await, a log, or any
    other yielding call under ``_cond`` on the strength of this note."""
    from gevent import monkey

    monkey.patch_all()


def _after_fork_in_child() -> None:
    """Fork-safety for EVERY child this process forks — a work-horse on the prefork
    pool, and the ``rq-scheduler`` process on every pool.

    Two inherited things are unusable in the child: the monitoring writer (its
    parent-owned background threads do not survive ``fork()`` and would hang flushes
    here; the child rebuilds a clean client on first use) and the fork gate's state.
    """
    # The fork happens INSIDE a job span, so the child inherits ``_live_spans >= 1``
    # (its own span, which only the parent will ever close) and possibly a condition
    # locked by a parent thread that does not exist here. Either would deadlock or
    # mis-block the child's first gate call, so reset to an empty gate.
    fork_gate.reset_after_fork()
    logger.info("forked child %s: evicting inherited monitoring client (fork safety); it rebuilds lazily", os.getpid())
    tai42_app.monitoring.active.writer.shutdown()


_fork_hooks_installed = False


def install_fork_hooks() -> None:
    """Register the after-fork child hook, once per process.

    EVERY pool needs this, not just prefork: ``work(with_scheduler=True)`` spawns the
    ``rq-scheduler`` child on solo and gevent too, and that child inherits the same
    unusable monitoring writer and fork-gate state. Called from :func:`_build_worker`,
    the one seam every pool passes through. Idempotent — ``os.register_at_fork`` has no
    deregister, so a second registration would run the hook twice per child.
    """
    global _fork_hooks_installed
    if _fork_hooks_installed:
        return
    os.register_at_fork(after_in_child=_after_fork_in_child)
    _fork_hooks_installed = True


def prepare_forking_worker() -> None:
    """Fork-safety setup specific to the PREFORK pool, before it starts forking a
    work-horse per job: drop the parent's monitoring client (its exporter thread would
    not survive ``fork()``) and on macOS disable system proxy detection (``urllib``'s
    proxy lookup deadlocks in a forked child during SSL setup).

    The after-fork child hook is NOT installed here — every pool needs it, so it is
    registered in :func:`install_fork_hooks` from the shared build seam.
    """
    if sys.platform == "darwin":
        logger.warning(
            "macOS forking worker: disabling system proxy detection "
            "(urllib.getproxies deadlocks in forked work-horses during SSL setup)"
        )
        os.environ["no_proxy"] = "*"
        urllib.request.getproxies = dict
    logger.warning(
        "forking worker: shutting down the monitoring writer before the first fork; "
        "each work-horse rebuilds its own client lazily"
    )
    tai42_app.monitoring.active.writer.shutdown()


def _build_worker(
    redis_url: str | None,
    name: str | None,
    results_ttl: int,
    pool: str,
) -> tuple[CustomRQWorker | CustomRQSimpleWorker, Redis]:
    """Build the RQ worker for the selected pool; returns it with its dedicated
    connection (closed by the caller after the worker exits).

    ``prefork`` (default) forks a monitored work-horse per job; ``solo`` and
    ``gevent`` run jobs in-process. ``results_ttl`` is the default result TTL; a
    ``name`` of ``None`` lets RQ auto-generate a unique per-process name.

    Every pool installs the after-fork child hook here: even the non-forking pools spawn
    the ``rq-scheduler`` child, which inherits the same unusable monitoring writer and
    fork-gate state. Only prefork additionally runs :func:`prepare_forking_worker`.
    """
    url = redis_url or rq_settings().redis_url
    install_fork_hooks()

    if pool == "gevent":
        setup_gevent()
        worker_class: type[CustomRQWorker | CustomRQSimpleWorker] = CustomRQSimpleWorker
    elif pool == "solo":
        worker_class = CustomRQSimpleWorker
    else:  # prefork
        worker_class = CustomRQWorker
        prepare_forking_worker()

    redis_conn = Redis.from_url(url)
    queue = Queue(connection=redis_conn)
    worker = worker_class([queue], name=name, connection=redis_conn, default_result_ttl=results_ttl)
    return worker, redis_conn


class RqWorkerRuntime(BackendRuntime):
    """RQ's work loop as ONE launchable runtime: the ``worker`` subcommand.

    Everything host-shaped around it — the readiness gate, the composed
    warm/cold shutdown wiring, the daemon thread the blocking loop runs on, and
    the drain the base requests when the launch is cancelled — belongs to
    ``ManagedBackend``. This class binds only RQ.

    ``pool_turnover_required`` stays False on every pool: prefork forks a FRESH
    work-horse per job, so the next horse inherits whatever this process's
    registry mutation applied, and the non-forking pools run jobs in this very
    process where the mutation already landed. There is no pool of persistent
    workers holding a stale snapshot, so there is nothing to turn over.
    """

    name = "worker"
    mode = ExecutionMode.worker_thread
    consumes_work = True
    pool_turnover_required = False

    def __init__(self, params: Mapping[str, Any]) -> None:
        self._params = dict(params)
        self._worker: CustomRQWorker | CustomRQSimpleWorker | None = None
        self._redis_conn: Redis | None = None

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        """Parse the worker CLI's own options, strictly: an unknown or malformed
        option raises out of click rather than being silently dropped."""
        ctx = main.make_context("rq-worker", list(args))
        return cls(ctx.params)

    async def build(self) -> None:
        """Construct the worker and its dedicated connection — the pool-specific
        fork-safety setup included. Runs before the readiness gate: building an
        engine object consumes nothing."""
        self._worker, self._redis_conn = _build_worker(
            self._params["redis_url"],
            self._params["name"],
            self._params["results_ttl"],
            self._params["pool"],
        )

    def run_blocking(self) -> None:
        """RQ's own work loop, on the daemon thread the base spawned."""
        self._require_worker().work(
            burst=self._params["burst"],
            logging_level=self._params["loglevel"].upper(),
            with_scheduler=True,
        )

    def request_drain(self) -> None:
        """Stop after the current job by setting rq's OWN stop flag, always.

        Never ``request_stop``: that method re-binds SIGTERM and SIGINT at the C
        level, which wipes asyncio's signal machinery and with it the host's
        composed chain — the main-task cancellation that guarantees teardown
        disappears, and a second SIGTERM then raises ``SystemExit`` out of a C
        handler and SIGKILLs the work-horse mid-job. It is also main-thread-only,
        while this call arrives on the loop thread with the work loop elsewhere.

        Setting the flag reaches the same place by the mixin's route: a busy
        worker stops after its job, and an idle one is woken by the bounded
        dequeue poll, which re-checks the flag and raises ``StopRequested``
        within ``_DEQUEUE_POLL_SECONDS``. Idempotent — re-setting a set flag is
        a no-op, which is what lets the base call this on the signal and again
        on the cancellation path.
        """
        worker = self._require_worker()
        logger.warning("rq worker %r: warm shutdown requested", worker.name)
        worker._stop_requested = True

    def request_terminate(self) -> None:
        """Cold stop on a repeated signal: kill the work-horse now.

        Only the prefork pool has one. The non-forking pools run the job in this
        process with nothing to kill, so the warm drain stands and the
        supervisor's kill is the backstop.
        """
        worker = self._require_worker()
        if worker.horse_pid:
            logger.warning("rq worker %r: cold shutdown — killing work-horse %s", worker.name, worker.horse_pid)
            worker.kill_horse()
            return
        logger.warning("rq worker %r: no work-horse to kill; the warm drain continues", worker.name)

    async def aclose(self) -> None:
        """Release the worker's dedicated Redis connection, on every exit path."""
        if self._redis_conn is not None:
            self._redis_conn.close()

    def _require_worker(self) -> CustomRQWorker | CustomRQSimpleWorker:
        """The built worker, or a loud refusal.

        The base always builds before it runs or drains, so reaching this is a
        broken lifecycle rather than a state to tolerate quietly.
        """
        if self._worker is None:
            raise RuntimeError("rq worker runtime was driven before build() constructed the worker")
        return self._worker


def start_rq_worker(
    redis_url: str | None,
    name: str | None,
    loglevel: str,
    burst: bool,
    results_ttl: int,
    pool: str,
) -> None:
    """Build and run the RQ worker inline, blocking the calling thread.

    The direct CLI path: on the main thread rq installs its own signal handlers, so
    warm/cold shutdown behaves exactly as a plain ``rq worker``. Nothing else runs
    in that process, so rq owning the signals is correct there. (The host
    ``launch`` path drives :class:`RqWorkerRuntime` instead, where the host owns
    them.)
    """
    worker, redis_conn = _build_worker(redis_url, name, results_ttl, pool)
    try:
        worker.work(burst=burst, logging_level=loglevel.upper(), with_scheduler=True)
    finally:
        redis_conn.close()


@click.command("tai42-backend-rq-worker")
@click.option("--redis-url", default=None, help="Redis URL (default: the RQ_REDIS_URL setting)")
@click.option(
    "--name",
    "-n",
    default=None,
    help="Worker name (default: an auto-generated unique name). RQ refuses a second "
    "active worker registered under an existing name, so a fixed name would block "
    "running multiple workers and restarting a SIGKILLed one within its registry TTL.",
)
@click.option("--loglevel", default="INFO", help="Log level")
@click.option("--burst", is_flag=True, help="Run in burst mode")
@click.option("--results-ttl", type=int, default=500, help="Default result TTL in seconds")
@click.option(
    "--pool",
    type=click.Choice(["prefork", "solo", "gevent"]),
    default="prefork",
    help="Pool type: prefork (forking), solo (no fork), gevent (green threads)",
)
def main(redis_url: str | None, name: str | None, loglevel: str, burst: bool, results_ttl: int, pool: str) -> None:
    """Run an RQ worker; ``launch(["worker", ...])`` parses its args here."""
    start_rq_worker(redis_url, name, loglevel, burst, results_ttl, pool)
