"""The RQ worker runtime: worker classes, fork safety, and the ``worker``
launch entrypoint.

``launch(["worker", ...])`` runs the blocking ``worker.work()`` loop on a worker
thread (:func:`run_rq_worker`), keeping the process's event loop responsive: the
app's bus subscription lives on that loop, so a blocked loop would starve op
delivery.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import threading
import time
import urllib.request
from typing import Any

import click
from redis import Redis
from rq import Queue, SimpleWorker, Worker
from rq.exceptions import StopRequested
from rq.timeouts import HorseMonitorTimeoutException, TimerDeathPenalty, UnixSignalDeathPenalty
from rq.worker import WorkerStatus
from tai42_contract.app import tai42_app

from tai42_backend_rq.settings import rq_settings

logger = logging.getLogger(__name__)

# Upper bound on one idle dequeue block, kept short so a warm-shutdown request
# (honored when the work loop next checks its stop flag) is not delayed by rq's
# default ``worker_ttl - 15`` poll off the main thread.
_DEQUEUE_POLL_SECONDS = 5

# How often the prefork parent re-checks whether its work-horse has exited, short
# enough to wake promptly and hit the ``job_monitoring_interval`` deadline in one tick.
_HORSE_POLL_SECONDS = 0.25

# Upper bound on the wait for the app to become ready before the work loop starts
# — a loud backstop for a boot that never completes; a worker that times out here
# refuses to fork against a half-built registry and exits.
_APP_READY_TIMEOUT_SECONDS = 120


class _TaiWorkerMixin:
    """Worker behavior shared by both worker classes.

    * Skips rq's signal-handler install when the work loop runs off the main
      thread (``signal.signal`` requires the main thread; shutdown then arrives
      via :func:`request_warm_shutdown`).
    * Bounds each idle dequeue poll (``_DEQUEUE_POLL_SECONDS``) AND returns to the
      outer ``work`` loop each poll so an off-thread warm-shutdown request is honored
      within seconds even while the worker is idle.
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
        """Honor an off-thread warm-shutdown request even while IDLE.

        rq's own idle shutdown relies on ``request_stop`` raising ``StopRequested`` INSIDE the
        blocked dequeue — a signal delivered on the worker's own (main) thread. Here the work
        loop runs off the main thread, so :func:`request_warm_shutdown` can only set
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


class CustomRQWorker(_TaiWorkerMixin, Worker):
    """The prefork worker: RQ's native forking ``execute_job`` (a monitored
    work-horse child per job, with timeout enforcement)."""

    def __init__(self, queues: Any, *args: Any, **kwargs: Any) -> None:
        super().__init__(queues, *args, **kwargs)
        # The horse pid ``kill_horse`` last targeted; the post-kill reap must not
        # self-timeout (see ``wait_for_horse``).
        self._killed_horse_pid = 0

    def fork_work_horse(self, job: Any, queue: Any) -> None:
        # Each new horse starts un-killed, so an OS-reused pid is never mistaken
        # for the killed one.
        self._killed_horse_pid = 0
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
    worker process itself."""

    def execute_job(self, job: Any, queue: Any) -> None:
        """Run the job in this process, refreshing the heartbeats while it runs.

        A non-forking worker has no horse monitor to refresh heartbeats during a
        job, so a refresher thread ticks them on the same
        ``job_monitoring_interval``, keeping both pools on one liveness contract.
        """
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
    I/O-bound jobs inside the single non-forking worker process."""
    from gevent import monkey

    monkey.patch_all()


def _after_fork_in_work_horse() -> None:
    """Fork-safety for every work-horse child: evict the monitoring writer (its
    parent-owned background threads do not survive ``fork()`` and would hang
    flushes here). The child rebuilds a clean client on first use."""
    logger.info("work-horse %s: evicting inherited monitoring client (fork safety); it rebuilds lazily", os.getpid())
    tai42_app.monitoring.active.writer.shutdown()


_fork_hooks_installed = False


def prepare_forking_worker() -> None:
    """One-time fork-safety setup before a prefork worker starts forking: drop the
    parent's monitoring client (its exporter thread would not survive ``fork()``),
    install the per-child evict hook, and on macOS disable system proxy detection
    (``urllib``'s proxy lookup deadlocks in a forked child during SSL setup)."""
    global _fork_hooks_installed
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
    if not _fork_hooks_installed:
        os.register_at_fork(after_in_child=_after_fork_in_work_horse)
        _fork_hooks_installed = True


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
    """
    url = redis_url or rq_settings().redis_url

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


def request_warm_shutdown(worker: Any) -> None:
    """Ask a worker whose work loop runs on another thread for a warm shutdown.

    On the main thread this drives rq's signal path: ``request_stop`` marks a busy
    worker to stop after the current job (idle, rq raises ``StopRequested``, so the
    stop flag is set directly). Off the main thread ``request_stop`` cannot run
    (its ``signal.signal`` re-bind is main-thread-only), so ``_stop_requested`` —
    the flag rq's own ``_shutdown`` sets — is set directly; the work loop honors it
    on its next dequeue poll (bounded by ``_DEQUEUE_POLL_SECONDS``).
    """
    logger.warning("rq worker %r: warm shutdown requested", worker.name)
    if threading.current_thread() is not threading.main_thread():
        worker._stop_requested = True
        return
    try:
        worker.request_stop(signal.SIGTERM, None)
    except StopRequested:
        worker._stop_requested = True


async def run_rq_worker(
    redis_url: str | None,
    name: str | None,
    loglevel: str,
    burst: bool,
    results_ttl: int,
    pool: str,
) -> None:
    """Run the RQ worker on a worker thread, keeping this event loop alive.

    The app's worker-bus subscription lives on this loop, so the blocking
    ``worker.work()`` runs off-loop to avoid starving op delivery. Loop-level
    SIGTERM/SIGINT handlers drive rq's warm shutdown; a cancellation of this
    coroutine requests the same shutdown and awaits the worker's exit.
    """
    # The tool registry is (re)built by the boot self-resync running concurrently
    # on the app loop; a worker consuming before it finishes would fork against a
    # half-built registry and fail jobs permanently. Await the readiness latch
    # first; a timeout is a boot that never completed — fail loudly.
    try:
        await asyncio.wait_for(tai42_app.lifecycle.wait_until_ready(), timeout=_APP_READY_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        raise RuntimeError(
            f"rq worker: the app did not become ready within {_APP_READY_TIMEOUT_SECONDS}s "
            "(boot self-resync never completed); refusing to consume jobs against a half-built tool registry"
        ) from exc

    worker, redis_conn = _build_worker(redis_url, name, results_ttl, pool)
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, request_warm_shutdown, worker)
        except (RuntimeError, ValueError, NotImplementedError) as exc:
            # Signal handlers need the main thread (and POSIX); without them
            # shutdown comes via task cancellation.
            logger.warning("cannot install %s handler for worker shutdown: %s", sig.name, exc)
        else:
            installed.append(sig)

    work = asyncio.ensure_future(
        asyncio.to_thread(worker.work, burst=burst, logging_level=loglevel.upper(), with_scheduler=True)
    )
    try:
        await asyncio.shield(work)
    except asyncio.CancelledError:
        request_warm_shutdown(worker)
        await work
        raise
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
        redis_conn.close()


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
    warm/cold shutdown behaves exactly as a plain ``rq worker``. (The host
    ``launch`` path uses :func:`run_rq_worker` instead.)
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
