"""The RQ worker runtime: fork safety, the pool's worker build, and the ``worker`` launch entrypoint.

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
thereafter), solo/gevent gate the whole in-process job — plus every scheduler spawn. The worker
classes that take that side live in :mod:`tai42_backend_rq.worker_pools`.
"""

from __future__ import annotations

import logging
import os
import sys
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Self

import click
from redis import Redis
from tai42_contract.app import tai42_app
from tai42_contract.backend.runtime import BackendRuntime, ExecutionMode, ProgressLiveness, ProgressReporter
from tai42_kit.clients.impl.redis import KitSyncRedis, redis_from_url_kwargs
from tai42_kit.fork_gate import fork_gate
from tai42_kit.liveness import LivenessFile, backend_liveness_settings

from tai42_backend_rq.queue import TaskBudgetQueue
from tai42_backend_rq.queues import check_queue_label, derived_queue
from tai42_backend_rq.settings import rq_client_kwargs, rq_settings
from tai42_backend_rq.worker_pools import (
    CustomRQGeventWorker,
    CustomRQSimpleWorker,
    CustomRQWorker,
)

logger = logging.getLogger(__name__)


def setup_gevent() -> None:
    """Monkey-patch blocking primitives so the gevent pool can overlap I/O-bound jobs in one worker process.

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
    other yielding call under ``_cond`` on the strength of this note.
    """
    from gevent import monkey

    monkey.patch_all()


def _after_fork_in_child() -> None:
    """Run fork-safety for EVERY child this process forks — a work-horse or the ``rq-scheduler`` process.

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
    """Run fork-safety setup specific to the PREFORK pool, before it forks a work-horse per job.

    Drops the parent's monitoring client (its exporter thread would not survive ``fork()``) and
    on macOS disables system proxy detection (``urllib``'s proxy lookup deadlocks in a forked
    child during SSL setup).

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


def rq_redis_connection(redis_url: str | None) -> KitSyncRedis:
    """A dedicated sync connection on the kit seam, for a process-long RQ loop.

    Built from :func:`~tai42_backend_rq.settings.rq_client_kwargs`, so the ``RQ_*``
    connection retry and socket timeouts reach every command the loop runs, the worker's
    pub/sub included; ``redis_url`` (a CLI override) replaces only the URL. The caller
    closes it.
    """
    return KitSyncRedis.from_url(
        **redis_from_url_kwargs({**rq_client_kwargs(), "url": redis_url or rq_settings().redis.redis_url}, sync=True)
    )


def _build_worker(
    redis_url: str | None,
    name: str | None,
    results_ttl: int,
    pool: str,
    label: str | None,
    report_progress: ProgressReporter,
) -> tuple[CustomRQWorker | CustomRQSimpleWorker, Redis]:
    """Build the RQ worker for the selected pool, returning it with its dedicated connection.

    The caller closes the returned connection after the worker exits.
    ``prefork`` (default) forks a monitored work-horse per job; ``solo`` and
    ``gevent`` run jobs in-process. ``results_ttl`` is the default result TTL; a
    ``name`` of ``None`` lets RQ auto-generate a unique per-process name. The worker
    serves one queue: ``<RQ_QUEUE_NAME>.<label>``, or ``RQ_QUEUE_NAME`` for no label,
    read from the settings here at build time. The worker reports each pass of its work
    loop to ``report_progress``.

    Every pool installs the after-fork child hook here: even the non-forking pools spawn
    the ``rq-scheduler`` child, which inherits the same unusable monitoring writer and
    fork-gate state. Only prefork additionally runs :func:`prepare_forking_worker`.
    """
    install_fork_hooks()

    if pool == "gevent":
        setup_gevent()
        worker_class: type[CustomRQWorker | CustomRQSimpleWorker] = CustomRQGeventWorker
    elif pool == "solo":
        worker_class = CustomRQSimpleWorker
    else:  # prefork
        worker_class = CustomRQWorker
        prepare_forking_worker()

    redis_conn = rq_redis_connection(redis_url)
    queue = TaskBudgetQueue(derived_queue(label), connection=redis_conn)
    worker = worker_class(
        [queue], name=name, connection=redis_conn, default_result_ttl=results_ttl, report_progress=report_progress
    )
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
    liveness = ProgressLiveness()

    def __init__(self, params: Mapping[str, Any]) -> None:
        """Store the launch ``params``; the worker and its connection are built in :meth:`build`."""
        self._params = dict(params)
        self._worker: CustomRQWorker | CustomRQSimpleWorker | None = None
        self._redis_conn: Redis | None = None

    @classmethod
    def from_args(cls, args: Sequence[str]) -> Self:
        """Parse the worker CLI's own options strictly; an unknown or malformed option raises out of click."""
        ctx = main.make_context("rq-worker", list(args))
        return cls(ctx.params)

    async def build(self) -> None:
        """Construct the worker and its dedicated connection, pool-specific fork-safety setup included.

        Runs before the readiness gate: building an engine object consumes nothing.
        """
        self._worker, self._redis_conn = _build_worker(
            self._params["redis_url"],
            self._params["name"],
            self._params["results_ttl"],
            self._params["pool"],
            self._params["queue"],
            self.report_progress,
        )

    def run_blocking(self) -> None:
        """RQ's own work loop, on the daemon thread the base spawned."""
        worker = self._require_worker()
        worker.work(burst=self._params["burst"], logging_level=self._params["loglevel"].upper(), with_scheduler=True)
        worker.raise_for_liveness_failure()

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
    queue: str | None,
) -> None:
    """Build and run the RQ worker inline, blocking the calling thread.

    The direct CLI path: on the main thread rq installs its own signal handlers, so
    warm/cold shutdown behaves exactly as a plain ``rq worker``. Nothing else runs
    in that process, so rq owning the signals is correct there. (The host
    ``launch`` path drives :class:`RqWorkerRuntime` instead, where the host owns
    them.)

    The work loop refreshes the liveness file (``TAI_BACKEND_LIVENESS_``) on every pass, as
    the host path does; the file is removed when the loop exits.
    """
    liveness = LivenessFile.from_settings(backend_liveness_settings())
    worker, redis_conn = _build_worker(redis_url, name, results_ttl, pool, queue, liveness.report)
    try:
        try:
            worker.work(burst=burst, logging_level=loglevel.upper(), with_scheduler=True)
        finally:
            redis_conn.close()
        worker.raise_for_liveness_failure()
    finally:
        liveness.remove()


def _queue_label_option(ctx: click.Context, param: click.Parameter, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return check_queue_label(value)
    except ValueError as exc:
        raise click.BadParameter(str(exc), ctx=ctx, param=param) from exc


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
@click.option(
    "--queue",
    default=None,
    callback=_queue_label_option,
    help="Queue label: serve <RQ_QUEUE_NAME>.<label> instead of RQ_QUEUE_NAME",
)
def main(
    redis_url: str | None,
    name: str | None,
    loglevel: str,
    burst: bool,
    results_ttl: int,
    pool: str,
    queue: str | None,
) -> None:
    """Run an RQ worker; ``launch(["worker", ...])`` parses its args here."""
    start_rq_worker(redis_url, name, loglevel, burst, results_ttl, pool, queue)
