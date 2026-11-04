"""The worker runtime: fork safety, the fork gate, worker classes, and the worker
start wiring."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import fakeredis
import pytest
from rq import Queue
from rq import scheduler as rq_scheduler
from rq.scheduler import RQScheduler
from rq.worker.base import BaseWorker
from tai42_kit.backend import fork_safety
from tai42_kit.fork_gate import JOB_SPAN_WAIT_SECONDS, fork_gate

from tai42_backend_rq import unpatched, worker_pools
from tai42_backend_rq import worker as worker_module
from tai42_backend_rq.job_loop import HostLoopJob

from .conftest import FakeRedisConn, FakeRqWorker


def _no_report(*, next_within: float) -> None:
    """A progress reporter that drops every report."""


# --- worker classes ----------------------------------------------------------


def test_worker_classes_compose_rq_workers():
    from rq import SimpleWorker, Worker

    assert issubclass(worker_pools.CustomRQWorker, Worker)
    assert issubclass(worker_pools.CustomRQSimpleWorker, SimpleWorker)
    assert issubclass(worker_pools.CustomRQWorker, worker_pools._TaiWorkerMixin)
    assert issubclass(worker_pools.CustomRQSimpleWorker, worker_pools._TaiWorkerMixin)
    assert issubclass(worker_pools.CustomRQGeventWorker, worker_pools.CustomRQSimpleWorker)


def _fakeredis_worker(cls: type) -> Any:
    """A real pool worker over an in-memory Redis."""
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    return cls([Queue("q", connection=conn)], connection=conn, report_progress=_no_report)


_POOL_CLASSES = [
    pytest.param(worker_pools.CustomRQWorker, id="prefork"),
    pytest.param(worker_pools.CustomRQSimpleWorker, id="solo"),
    pytest.param(worker_pools.CustomRQGeventWorker, id="gevent"),
]


@pytest.mark.skipif(
    threading.current_thread() is not threading.main_thread(), reason="signals install on the main thread"
)
@pytest.mark.parametrize("cls", _POOL_CLASSES)
def test_no_pool_replaces_the_host_s_signal_handlers_even_on_the_main_thread(cls: type):
    """The host owns the process's signals: rq's install is skipped where ``signal.signal`` would succeed."""
    worker = _fakeredis_worker(cls)
    before = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}
    try:
        worker._install_signal_handlers()
        assert {signum: signal.getsignal(signum) for signum in before} == before
    finally:
        for signum, handler in before.items():
            signal.signal(signum, handler)


# A gevent-patched process, where a ``threading`` thread is a greenlet of the main OS thread and
# ``signal.signal`` succeeds in it: the gevent worker's install, made from such a thread, prints
# whether that thread could install a handler and whether SIGINT and SIGTERM kept their handlers.
_GEVENT_SIGNAL_SCRIPT = textwrap.dedent(
    """
    from gevent import monkey

    monkey.patch_all()

    import json, signal, sys, threading

    sys.path.insert(0, sys.argv[1])
    import conftest  # noqa: F401
    import fakeredis
    from rq import Queue

    from tai42_backend_rq import worker_pools

    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    worker = worker_pools.CustomRQGeventWorker(
        [Queue("q", connection=conn)], connection=conn, report_progress=lambda *, next_within: None
    )
    before = {int(s): signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
    seen = {}

    def install():
        previous = signal.signal(signal.SIGUSR1, signal.SIG_IGN)
        signal.signal(signal.SIGUSR1, previous)
        seen["thread_can_install"] = True
        worker._install_signal_handlers()

    thread = threading.Thread(target=install)
    thread.start()
    thread.join()
    seen["kept"] = {int(s): signal.getsignal(s) for s in before} == before
    print(json.dumps(seen))
    """
)


def test_the_gevent_pool_s_work_loop_thread_replaces_no_signal_handler():
    """Under the gevent patch the work loop's thread is a greenlet of the main OS thread, where rq's
    install would succeed: it is skipped there too."""
    result = subprocess.run(
        [sys.executable, "-c", _GEVENT_SIGNAL_SCRIPT, os.path.dirname(__file__)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"thread_can_install": True, "kept": True}


def test_mixin_bounds_the_dequeue_poll():
    class Base:
        def __init__(self, queues: Any, *args: Any, **kwargs: Any) -> None:
            self.name = kwargs["name"]

    class TestWorker(worker_pools._TaiWorkerMixin, Base):
        pass

    assert (
        TestWorker(["default"], name="w1", report_progress=_no_report).dequeue_timeout
        == worker_pools._DEQUEUE_POLL_SECONDS
    )


# --- fork safety -------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
@pytest.mark.parametrize("cls", _POOL_CLASSES)
def test_every_child_the_process_forks_evicts_its_inherited_monitoring(app, cls: type):
    """A built worker's fork safety reaches every child of the process, whoever forks it."""
    _fakeredis_worker(cls).guard_forks()
    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        os.write(write_end, str(app.monitoring.active.writer.shutdown_calls).encode())
        os._exit(0)
    os.close(write_end)
    try:
        shutdowns = os.read(read_end, 16)
    finally:
        os.close(read_end)
        os.waitpid(pid, 0)

    assert int(shutdowns) == app.monitoring.active.writer.shutdown_calls + 1


def test_work_horse_perform_job_flushes_monitoring(app, monkeypatch):
    """The work-horse exits via os._exit, so the job path must flush buffered
    spans explicitly after the job ran."""
    ran: list[str] = []
    monkeypatch.setattr(worker_pools.Worker, "perform_job", lambda self, job, queue: ran.append("job") or True)
    horse = object.__new__(worker_pools.CustomRQWorker)

    assert horse.perform_job("job", "queue") is True
    assert ran == ["job"]
    assert app.monitoring.active.writer.flush_calls == 1


def test_work_horse_flushes_even_when_the_job_raises(app, monkeypatch):
    def boom(self: Any, job: Any, queue: Any) -> bool:
        raise RuntimeError("job blew up")

    monkeypatch.setattr(worker_pools.Worker, "perform_job", boom)
    horse = object.__new__(worker_pools.CustomRQWorker)

    with pytest.raises(RuntimeError, match="job blew up"):
        horse.perform_job("job", "queue")
    assert app.monitoring.active.writer.flush_calls == 1


def test_work_horse_flush_failure_is_logged_not_raised(app, monkeypatch, caplog):
    monkeypatch.setattr(worker_pools.Worker, "perform_job", lambda self, job, queue: True)

    def failing_flush() -> None:
        raise RuntimeError("flush broke")

    monkeypatch.setattr(app.monitoring.active.writer, "flush", failing_flush)
    horse = object.__new__(worker_pools.CustomRQWorker)

    with caplog.at_level("ERROR"):
        assert horse.perform_job("job", "queue") is True
    assert any("monitoring flush failed" in record.message for record in caplog.records)


# --- the fork gate: no job child alive during a manifest re-import -----------


@pytest.mark.parametrize("cls", _POOL_CLASSES)
def test_a_worker_guards_every_fork(cls):
    """Every pool installs the kit's fork guard with the worker's own settings: a fork that waits on
    a re-import waits up to ``JOB_SPAN_WAIT_SECONDS``, and every child evicts the monitoring writer
    it inherited."""
    worker = object.__new__(cls)

    worker.guard_forks()

    guard = fork_gate._fork_hooks.guard
    assert guard is not None
    assert guard.timeout == JOB_SPAN_WAIT_SECONDS
    assert guard.after_in_child is fork_safety.evict_inherited_monitoring


def _fork_the_work_horse(
    monkeypatch: pytest.MonkeyPatch, worker: Any, in_child: Callable[[], None]
) -> Callable[[], object]:
    """rq forks the work-horse for a job, whose run is ``in_child``; returns the reap of the horse."""
    monkeypatch.setattr(worker_pools.Worker, "main_work_horse", lambda self, job, queue: in_child())
    # rq names the worker and the job in the parent's environment as it forks.
    monkeypatch.setenv("RQ_WORKER_ID", "")
    monkeypatch.setenv("RQ_JOB_ID", "")
    worker.fork_work_horse(SimpleNamespace(id="j"), "queue")
    return lambda: os.waitpid(worker.horse_pid, 0)


def _start_the_scheduler(
    monkeypatch: pytest.MonkeyPatch, worker: Any, in_child: Callable[[], None]
) -> Callable[[], object]:
    """rq starts its scheduler process with the work loop, whose run is ``in_child``; returns its join."""
    monkeypatch.setattr(rq_scheduler, "run", lambda scheduler: in_child())
    worker._start_scheduler(False, "INFO")
    return lambda: worker.scheduler._process.join(5.0)


def _respawn_the_scheduler(
    monkeypatch: pytest.MonkeyPatch, worker: Any, in_child: Callable[[], None]
) -> Callable[[], object]:
    """rq's maintenance pass re-spawns a scheduler that is not running, whose run is ``in_child``; returns its join."""
    monkeypatch.setattr(rq_scheduler, "run", lambda scheduler: in_child())
    worker.scheduler = RQScheduler(worker.queues, connection=worker.connection)
    worker.last_cleaned_at = datetime.now(UTC)
    worker.run_maintenance_tasks()
    return lambda: worker.scheduler._process.join(5.0)


# Each fork the prefork worker makes itself, through rq's own code: the work-horse, the scheduler
# process rq starts with the work loop (``RQScheduler.start``, a ``multiprocessing`` fork process),
# and the scheduler re-spawn from rq's maintenance pass.
_OWN_FORKS = [
    pytest.param(_fork_the_work_horse, id="work-horse"),
    pytest.param(_start_the_scheduler, id="scheduler-start"),
    pytest.param(_respawn_the_scheduler, id="maintenance"),
]


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
@pytest.mark.parametrize("fork", _OWN_FORKS)
def test_a_fork_the_worker_makes_waits_out_a_running_reimport_reporting_its_progress(
    app, monkeypatch, fork: Callable[..., Callable[[], object]]
):
    """The fork guard holds each fork rq makes for the worker until a running re-import ends, and the
    worker reports its progress each tick meanwhile: rq makes each of them in its work loop, on the
    loop's own thread."""
    reports: list[float] = []
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    worker = worker_pools.CustomRQWorker(
        [Queue("q", connection=conn)],
        name="w",
        connection=conn,
        report_progress=lambda *, next_within: reports.append(next_within),
    )
    worker.guard_forks()
    read_end, write_end = os.pipe()

    def in_child() -> None:  # pragma: no cover - runs in the child
        os.write(write_end, str(time.monotonic()).encode())

    held = threading.Event()
    released_at: list[float] = []

    def rebuild() -> None:
        with fork_gate.exclusive(timeout=5.0):
            held.set()
            time.sleep(1.5)
            released_at.append(time.monotonic())

    rebuilder = threading.Thread(target=rebuild, daemon=True)
    rebuilder.start()
    assert held.wait(5.0)
    reaps: list[Callable[[], object]] = []

    def one_pass_forking(self: Any, *args: Any, **kwargs: Any) -> bool:
        reaps.append(fork(monkeypatch, self, in_child))
        return True

    monkeypatch.setattr(worker_pools.Worker, "work", one_pass_forking)
    worker.work()
    (reap,) = reaps
    os.close(write_end)
    try:
        child_ran_at = os.read(read_end, 64)
    finally:
        os.close(read_end)
        reap()
    rebuilder.join(5.0)

    assert float(child_ran_at) >= released_at[0]
    assert reports
    assert set(reports) == {worker_pools._REFRESH_TICK_SECONDS}
    assert fork_gate.live_spans == 0
    assert fork_gate._fork_hooks.fork_spans == {}


# The scheduler start and the maintenance pass of the forking pools: each reaches rq's own body,
# which forks at most once and otherwise does bounded Redis work.
_SCHEDULER_SEAMS = [
    pytest.param(cls, seam, args, id=f"{cls_id}-{seam_id}")
    for cls, cls_id in ((worker_pools.CustomRQWorker, "prefork"), (worker_pools.CustomRQSimpleWorker, "solo"))
    for seam, args, seam_id in (
        ("_start_scheduler", (False, "INFO"), "scheduler-start"),
        ("run_maintenance_tasks", (), "maintenance"),
    )
]


@pytest.mark.parametrize(("cls", "seam", "args"), _SCHEDULER_SEAMS)
def test_a_scheduler_seam_that_forks_nothing_holds_no_job_span(
    monkeypatch, cls: type, seam: str, args: tuple[Any, ...]
):
    """Only a fork instant is gated: the Redis work around it holds no config reload up."""
    spans: list[int] = []
    monkeypatch.setattr(BaseWorker, seam, lambda self, *a, **kw: spans.append(fork_gate.live_spans))
    worker = object.__new__(cls)

    getattr(worker, seam)(*args)

    assert spans == [0]


# What the gevent worker's scheduler start and maintenance pass reach: it runs the scheduler as a thread and
# forks nothing.
_GEVENT_SCHEDULER_SEAMS = [
    pytest.param("_start_scheduler", (False, "INFO"), RQScheduler, "acquire_locks", id="scheduler-start"),
    pytest.param("run_maintenance_tasks", (), worker_pools.SimpleWorker, "run_maintenance_tasks", id="maintenance"),
]


@pytest.mark.parametrize(("seam", "args", "owner", "reached"), _GEVENT_SCHEDULER_SEAMS)
def test_the_gevent_worker_s_scheduler_seams_take_no_fork_span(
    monkeypatch, seam: str, args: tuple[Any, ...], owner: type, reached: str
):
    spans: list[int] = []

    def record(self: Any, *a: Any, **kw: Any) -> None:
        spans.append(fork_gate.live_spans)

    monkeypatch.setattr(owner, reached, record)
    worker = _fakeredis_worker(worker_pools.CustomRQGeventWorker)

    getattr(worker, seam)(*args)

    assert spans == [0]


def test_fork_work_horse_starts_each_horse_un_killed(monkeypatch):
    monkeypatch.setattr(worker_pools.Worker, "fork_work_horse", lambda self, job, queue: None)
    worker = object.__new__(worker_pools.CustomRQWorker)
    worker._killed_horse_pid = 4242

    worker.fork_work_horse("job", "queue")

    assert worker._killed_horse_pid == 0


def test_prefork_does_not_hold_the_span_across_the_horses_whole_run(monkeypatch):
    """The span must NOT cover fork→reap: a horse already forked is immune to later
    re-imports, so holding the gate for its whole job would block reloads behind
    arbitrarily long jobs for no protection."""
    during_monitor: list[int] = []
    monkeypatch.setattr(worker_pools.Worker, "fork_work_horse", lambda self, job, queue: None)
    monkeypatch.setattr(
        worker_pools.Worker, "monitor_work_horse", lambda self, job, queue: during_monitor.append(fork_gate.live_spans)
    )
    # rq's execute_job is prepare -> fork -> monitor -> set_state; the two that touch
    # Redis are stubbed so the ordering assertion needs no live connection.
    monkeypatch.setattr(worker_pools.Worker, "prepare_execution", lambda self, job: None)
    monkeypatch.setattr(worker_pools.Worker, "set_state", lambda self, state, pipeline=None: None)
    worker = cast("Any", object.__new__(worker_pools.CustomRQWorker))

    # rq's own execute_job runs unwrapped: only the fork instant inside it is gated.
    worker.execute_job(SimpleNamespace(id="job", timeout=None), "queue")
    assert during_monitor == [0], "the gate is still held while the horse runs"
    assert fork_gate.live_spans == 0


def test_simple_pool_holds_the_span_across_the_whole_in_process_job(monkeypatch):
    """The non-forking pools have no child and no inherited snapshot: the job body
    imports against live ``sys.modules`` for its whole duration, so the span covers it
    all. It must NOT route through the prefork fork seam."""
    spans: list[int] = []
    monkeypatch.setattr(
        worker_pools.CustomRQSimpleWorker,
        "_run_job_inline",
        lambda self, job, queue: spans.append(fork_gate.live_spans),
    )
    worker = object.__new__(worker_pools.CustomRQSimpleWorker)

    worker.execute_job("job", "queue")
    assert spans == [1]
    assert fork_gate.live_spans == 0
    assert not hasattr(worker, "fork_work_horse")


def test_simple_pool_releases_its_span_when_the_job_body_raises(monkeypatch):
    def boom(self: Any, job: Any, queue: Any) -> None:
        raise RuntimeError("job blew up")

    monkeypatch.setattr(worker_pools.CustomRQSimpleWorker, "_run_job_inline", boom)
    worker = object.__new__(worker_pools.CustomRQSimpleWorker)

    with pytest.raises(RuntimeError, match="job blew up"):
        worker.execute_job("job", "queue")
    assert fork_gate.live_spans == 0


def test_prepare_forking_worker_darwin_disables_proxy_detection(app, monkeypatch):
    import urllib.request

    monkeypatch.setattr(worker_module.sys, "platform", "darwin")
    monkeypatch.setenv("no_proxy", "original")
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies)

    worker_module.prepare_forking_worker()
    assert os.environ["no_proxy"] == "*"
    assert urllib.request.getproxies() == {}


# --- worker build -----------------------------------------------------------


class FakeQueue:
    def __init__(self, name: Any = None, connection: Any = None) -> None:
        self.name = name
        self.connection = connection


def _fake_worker_class(record: dict[str, Any]) -> type:
    class FakeWorker:
        name = "fake"

        def __init__(
            self, queues: Any, name: str | None, connection: Any, default_result_ttl: int, report_progress: Any
        ) -> None:
            record["queues"] = queues
            record["report_progress"] = report_progress
            record["name"] = name
            record["connection"] = connection
            record["default_result_ttl"] = default_result_ttl
            self.name = name

        def guard_forks(self) -> None:
            record["guarded"] = True

    return FakeWorker


@pytest.fixture
def worker_env(monkeypatch) -> dict[str, Any]:
    record: dict[str, Any] = {"prepared": 0}
    conn = FakeRedisConn()
    record["conn"] = conn

    def _from_url(**kwargs: Any) -> FakeRedisConn:
        record["from_url"] = kwargs
        return conn

    monkeypatch.setattr(worker_module, "KitSyncRedis", type("R", (), {"from_url": staticmethod(_from_url)}))
    monkeypatch.setattr(worker_module, "TaskBudgetQueue", FakeQueue)
    monkeypatch.setattr(
        worker_module, "prepare_forking_worker", lambda: record.__setitem__("prepared", record["prepared"] + 1)
    )
    fake_cls = _fake_worker_class(record)
    monkeypatch.setattr(worker_module, "CustomRQWorker", fake_cls)
    monkeypatch.setattr(worker_module, "CustomRQSimpleWorker", fake_cls)
    monkeypatch.setattr(worker_module, "CustomRQGeventWorker", fake_cls)
    return record


def test_build_worker_prepares_the_prefork_pool(worker_env):
    _worker, conn = worker_module._build_worker(None, "w1", 500, "prefork", None, _no_report)
    assert worker_env["prepared"] == 1
    assert worker_env["guarded"] is True
    assert worker_env["name"] == "w1"
    # The worker consumes the queue named by the ``queue_name`` setting (default
    # ``default``), the same queue the enqueue side and scheduler bind.
    assert worker_env["queues"][0].name == "default"
    # --results-ttl reaches rq as the worker's default result TTL.
    assert worker_env["default_result_ttl"] == 500
    assert conn is worker_env["conn"]


def test_the_worker_connection_carries_the_kit_sync_retry(worker_env, monkeypatch):
    """The worker's own connection is built by the kit seam: the ``RQ_*`` retry and socket
    timeouts reach every command the worker loop runs, read as bytes, and its pub/sub is
    the kit's re-entrant one."""
    from tai42_kit.clients.impl.redis import KitSyncRedis, ReentrantPubSub, _LoggedSyncRetry

    from tai42_backend_rq.settings import rq_settings

    monkeypatch.setattr(worker_module, "KitSyncRedis", KitSyncRedis)
    monkeypatch.setenv("RQ_REDIS_URL", "redis://configured:6390/2")
    monkeypatch.setenv("RQ_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("RQ_SOCKET_TIMEOUT", "3")
    rq_settings.cache_clear()
    try:
        _worker, conn = worker_module._build_worker(None, "w1", 500, "solo", None, _no_report)
    finally:
        rq_settings.cache_clear()

    assert isinstance(conn, KitSyncRedis)
    assert isinstance(conn.pubsub(), ReentrantPubSub)
    connection_kwargs = conn.connection_pool.connection_kwargs
    assert (connection_kwargs["host"], connection_kwargs["port"], connection_kwargs["db"]) == ("configured", 6390, 2)
    assert connection_kwargs["decode_responses"] is False
    assert connection_kwargs["socket_timeout"] == 3
    retry = connection_kwargs["retry"]
    assert isinstance(retry, _LoggedSyncRetry)
    assert retry._budget == 2
    assert retry._label == "RQ_REDIS_URL"
    conn.close()


def test_the_redis_url_flag_wins_for_the_worker_connection(worker_env):
    worker_module._build_worker("redis://custom:6391/3", "w2", 100, "solo", None, _no_report)

    assert worker_env["from_url"]["url"] == "redis://custom:6391/3"
    assert worker_env["from_url"]["decode_responses"] is False


def test_build_worker_builds_the_solo_pool_s_worker_without_forking_setup(worker_env):
    worker_module._build_worker("redis://custom", "w2", 100, "solo", None, _no_report)
    assert worker_env["prepared"] == 0
    # The solo pool still spawns the rq-scheduler child, so it needs the fork safety.
    assert worker_env["guarded"] is True
    assert worker_env["default_result_ttl"] == 100


def test_build_worker_builds_the_gevent_pool_s_worker_without_forking_setup(worker_env):
    worker_module._build_worker(None, "w3", 500, "gevent", None, _no_report)
    assert worker_env["prepared"] == 0
    assert worker_env["guarded"] is True


def test_the_worker_options_leave_the_worker_name_to_rq():
    """Parsed with no ``--name``, the name resolves to None, so RQ auto-generates a
    unique name per worker process. A fixed default would make RQ's ``register_birth``
    reject a second worker under the same name — no multi-worker fleet, and no restart
    of a killed worker while its registry entry is still around."""
    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args([]))

    assert runtime._params["name"] is None


def test_build_worker_hands_a_none_name_to_the_worker_class(worker_env):
    # The None reaches the rq worker class as ``name=None`` — that is what makes
    # rq generate the unique name; anything defaulted along the way would not.
    worker_module._build_worker(None, None, 500, "solo", None, _no_report)

    assert worker_env["name"] is None


# --- the worker runtime: the vendor binding the shared lifecycle drives ---------


def _built_runtime(args: list[str] | None = None, worker: Any = None, conn: Any = None) -> Any:
    """A runtime as it stands after ``build()`` — with the engine objects the
    base would have had it construct, injected."""
    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args(args or []))
    runtime._worker = worker if worker is not None else FakeRqWorker()
    runtime._redis_conn = conn
    return runtime


def test_the_worker_runtime_declares_a_drainable_off_loop_consumer():
    runtime_cls = worker_module.RqWorkerRuntime
    assert runtime_cls.name == "worker"
    # The blocking work loop may not sit on the serving loop: the app's bus
    # subscription lives there.
    assert runtime_cls.mode is worker_module.ExecutionMode.worker_thread
    assert runtime_cls.consumes_work is True
    # rq forks a FRESH work-horse per job (and the non-forking pools run jobs in
    # this process), so a registry mutation always reaches the next job. No
    # persistent pool holds a stale snapshot, so there is nothing to turn over.
    assert runtime_cls.pool_turnover_required is False


def test_from_args_parses_the_worker_cli_options():
    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args(["--pool", "solo", "-n", "w1", "--burst"]))

    assert runtime._params == {
        "redis_url": None,
        "name": "w1",
        "loglevel": "INFO",
        "burst": True,
        "results_ttl": 500,
        "pool": "solo",
        "queue": None,
    }


def test_from_args_declares_gevent_s_patch_as_the_gevent_pool_s_process_prelude():
    runtime = worker_module.RqWorkerRuntime.from_args(["--pool", "gevent"])

    assert runtime.process_prelude == "gevent.monkey:patch_all"


@pytest.mark.parametrize("args", [[], ["--pool", "prefork"], ["--pool", "solo"]])
def test_from_args_declares_no_process_prelude_for_the_other_pools(args: list[str]):
    assert worker_module.RqWorkerRuntime.from_args(args).process_prelude is None


def test_from_args_rejects_an_unknown_option():
    """Strict parsing: a typo aborts the launch loudly instead of starting a
    worker configured differently than the operator asked for."""
    import click

    with pytest.raises(click.UsageError):
        worker_module.RqWorkerRuntime.from_args(["--frobnicate"])


def test_from_args_starts_nothing(monkeypatch):
    built: list[Any] = []
    monkeypatch.setattr(worker_module, "_build_worker", lambda *args: built.append(args))

    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args([]))

    assert built == []
    assert runtime._worker is None


async def test_build_constructs_the_worker_and_its_connection(monkeypatch):
    worker = FakeRqWorker()
    conn = FakeRedisConn()
    calls: list[Any] = []
    monkeypatch.setattr(worker_module, "_build_worker", lambda *args: calls.append(args) or (worker, conn))

    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args(["--pool", "gevent", "--redis-url", "redis://x/1"]))
    await runtime.build()

    assert calls == [("redis://x/1", None, 500, "gevent", None, runtime.report_progress)]
    assert runtime._worker is worker
    assert runtime._redis_conn is conn
    shared = HostLoopJob._shared_loop
    assert shared is not None
    assert shared._loop is asyncio.get_running_loop()


@pytest.mark.parametrize("pool", ["prefork", "solo"])
async def test_build_hands_the_host_s_loop_to_no_job_on_the_other_pools(monkeypatch, pool: str):
    monkeypatch.setattr(worker_module, "_build_worker", lambda *args: (FakeRqWorker(), FakeRedisConn()))

    await cast("Any", worker_module.RqWorkerRuntime.from_args(["--pool", pool])).build()

    assert HostLoopJob._shared_loop is None


def test_run_blocking_runs_rqs_own_work_loop():
    worker = FakeRqWorker()
    worker._stop_requested = True  # return immediately
    runtime = _built_runtime(["--burst", "--loglevel", "debug"], worker=worker)

    runtime.run_blocking()

    assert worker.work_kwargs == {"burst": True, "logging_level": "DEBUG", "with_scheduler": True}


def test_request_drain_sets_rqs_stop_flag_and_never_calls_request_stop():
    """``request_stop`` re-binds SIGTERM/SIGINT at the C level: it removes asyncio's
    signal machinery, and with it the host's chain and the main-task cancellation
    that guarantees ``app_context`` teardown. The drain sets rq's own flag instead —
    the same flag rq's ``_shutdown`` sets."""
    worker = FakeRqWorker()
    runtime = _built_runtime(worker=worker)

    runtime.request_drain()

    assert worker._stop_requested is True
    assert worker.stop_requests == []


def test_request_drain_is_idempotent():
    """The base calls it on the signal AND again on the cancellation path, so a
    second call must not escalate or fail."""
    worker = FakeRqWorker()
    runtime = _built_runtime(worker=worker)

    runtime.request_drain()
    runtime.request_drain()

    assert worker._stop_requested is True
    assert worker.stop_requests == []
    assert worker.kills == 0


def test_request_drain_works_off_the_main_thread():
    """It must not touch main-thread-only APIs: the host may call it from any
    thread it drives the lifecycle on, and ``request_stop`` would raise here."""
    worker = FakeRqWorker()
    runtime = _built_runtime(worker=worker)

    thread = threading.Thread(target=runtime.request_drain)
    thread.start()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert worker._stop_requested is True
    assert worker.stop_requests == []


def test_an_idle_worker_stops_within_the_dequeue_poll_bound():
    """The other half of the drain binding: the flag alone stops an IDLE worker.

    rq's own idle stop depends on ``request_stop`` raising ``StopRequested``
    inside the blocked dequeue — the very call the runtime must not make. The
    mixin bounds each poll instead (``max_idle_time`` = ``dequeue_timeout``), so
    a flag set from the loop thread is seen on the next poll and unwinds the
    work loop.
    """
    from rq.exceptions import StopRequested

    class Base:
        def __init__(self) -> None:
            self.name = "w1"
            self.polls: list[Any] = []
            self.polling = threading.Event()

        def dequeue_job_and_maintain_ttl(self, timeout: Any, max_idle_time: Any = None) -> Any:
            self.polls.append((timeout, max_idle_time))
            self.polling.set()
            time.sleep(0.01)
            return None  # an idle poll that found no job

    class IdleWorker(worker_pools._TaiWorkerMixin, Base):
        pass

    worker = IdleWorker(report_progress=_no_report)
    worker._stop_requested = False
    runtime = _built_runtime(worker=worker)

    stopped = threading.Event()

    def work_loop() -> None:
        try:
            worker.dequeue_job_and_maintain_ttl(None)
        except StopRequested:
            stopped.set()

    thread = threading.Thread(target=work_loop, daemon=True)
    thread.start()
    assert worker.polling.wait(timeout=5), "the work loop never reached its dequeue poll"

    runtime.request_drain()

    assert stopped.wait(timeout=5), "the idle worker never honored the drain"
    # Each poll is bounded, which is what makes the flag observable at all.
    assert worker.polls[0] == (worker_pools._DEQUEUE_POLL_SECONDS, worker_pools._DEQUEUE_POLL_SECONDS)


def test_request_terminate_kills_the_work_horse():
    worker = FakeRqWorker(horse_pid=4242)
    runtime = _built_runtime(worker=worker)

    runtime.request_terminate()

    assert worker.kills == 1


def test_request_terminate_without_a_horse_leaves_the_drain_running(caplog):
    """The non-forking pools run the job in this process: there is nothing to
    kill, so the warm drain stands and the supervisor's kill is the backstop."""
    worker = FakeRqWorker(horse_pid=0)
    runtime = _built_runtime(worker=worker)

    with caplog.at_level("WARNING"):
        runtime.request_terminate()

    assert worker.kills == 0
    assert any("no work-horse to kill" in record.message for record in caplog.records)


async def test_aclose_closes_the_connection_the_runtime_owns():
    conn = FakeRedisConn()
    runtime = _built_runtime(conn=conn)

    await runtime.aclose()

    assert conn.closed


async def test_aclose_before_a_connection_exists_is_a_no_op():
    """``aclose`` is awaited on every exit path, including one that never got
    past ``build``."""
    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args([]))

    await runtime.aclose()


def test_driving_the_runtime_before_build_is_refused_loudly():
    runtime = cast("Any", worker_module.RqWorkerRuntime.from_args([]))

    with pytest.raises(RuntimeError, match="before build"):
        runtime.request_drain()


# ---- liveness: the heartbeats a BUSY worker must keep refreshing -------------


class _HeartbeatSpy:
    """Captures ``maintain_heartbeats`` calls without a real Redis or job."""

    def __init__(self) -> None:
        self.beats = 0
        self.job_monitoring_interval = 0.02

    def maintain_heartbeats(self, job: Any) -> None:
        self.beats += 1


def test_prefork_horse_monitor_ticks_while_the_job_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prefork parent's monitor must keep cycling while the horse works.

    RQ ticks it by raising ``HorseMonitorTimeoutException`` out of ``wait_for_horse``
    and refreshing the heartbeats in the handler. The parent runs off the main thread,
    where only a timer death penalty can be armed and its async exception cannot
    interrupt the blocking ``os.wait4`` — so ``wait_for_horse`` must time ITSELF out.
    Without that the worker refreshes no heartbeat for the whole job: the census drops
    a live worker and RQ abandons the execution mid-run.
    """
    worker = worker_pools.CustomRQWorker.__new__(worker_pools.CustomRQWorker)
    worker.job_monitoring_interval = 0.05  # type: ignore[attr-defined]
    worker._horse_pid = 424242  # type: ignore[attr-defined]
    worker._killed_horse_pid = 0  # type: ignore[attr-defined]
    worker._report_progress = _no_report  # type: ignore[attr-defined]

    # A horse that has not exited: WNOHANG keeps reporting "not reaped yet".
    monkeypatch.setattr(worker_pools.os, "wait4", lambda pid, flags: (0, 0, None))
    started = time.monotonic()
    with pytest.raises(worker_pools.HorseMonitorTimeoutException):
        worker.wait_for_horse()
    # It timed itself out on real time, promptly — not after the job ended.
    assert time.monotonic() - started < 1.0


def test_prefork_horse_monitor_returns_the_reaped_horse(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = worker_pools.CustomRQWorker.__new__(worker_pools.CustomRQWorker)
    worker.job_monitoring_interval = 5  # type: ignore[attr-defined]
    worker._horse_pid = 424242  # type: ignore[attr-defined]
    worker._killed_horse_pid = 0  # type: ignore[attr-defined]
    worker._report_progress = _no_report  # type: ignore[attr-defined]
    monkeypatch.setattr(worker_pools.os, "wait4", lambda pid, flags: (424242, 0, "rusage"))

    assert worker.wait_for_horse() == (424242, 0, "rusage")


def test_work_horse_child_restores_the_signal_death_penalty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only SIGALRM can interrupt job code blocked in a C call, and the forking
    thread becomes the child's main thread — so the horse enforces job timeouts with
    RQ's signal penalty even though the parent cannot arm one."""
    worker = worker_pools.CustomRQWorker.__new__(worker_pools.CustomRQWorker)
    monkeypatch.setattr(worker_pools.Worker, "main_work_horse", lambda self, job, queue: None)

    worker.main_work_horse(object(), object())

    assert worker.death_penalty_class is worker_pools.UnixSignalDeathPenalty


def test_simple_worker_refreshes_heartbeats_while_a_job_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-forking worker has no monitor process, so nothing would refresh its
    heartbeats for the job's whole duration — a long job would read as a dead worker.
    Its heartbeat thread is what keeps both pools on ONE liveness contract."""
    worker = worker_pools.CustomRQSimpleWorker.__new__(worker_pools.CustomRQSimpleWorker)
    spy = _HeartbeatSpy()
    worker.job_monitoring_interval = spy.job_monitoring_interval  # type: ignore[attr-defined]
    worker._report_progress = _no_report  # type: ignore[attr-defined]
    worker._execution_guard = unpatched.os_thread_lock()  # type: ignore[attr-defined]
    worker._run_guard = unpatched.os_thread_lock()  # type: ignore[attr-defined]
    worker._running_job_id = None  # type: ignore[attr-defined]
    worker._stopped_job_id = None  # type: ignore[attr-defined]
    monkeypatch.setattr(worker_pools, "_REFRESH_TICK_SECONDS", 0.005)
    monkeypatch.setattr(type(worker), "maintain_heartbeats", lambda self, job: spy.maintain_heartbeats(job))
    monkeypatch.setattr(type(worker), "prepare_execution", lambda self, job: setattr(self, "execution", job))
    monkeypatch.setattr(type(worker), "set_state", lambda self, state: None)
    monkeypatch.setattr(type(worker), "perform_job", lambda self, job, queue: time.sleep(0.2) or True)

    worker.execute_job(SimpleNamespace(id="job-1", timeout=300), object())

    assert spy.beats >= 2, "a busy non-forking worker never refreshed its heartbeat"


def test_wait_for_horse_after_kill_does_not_self_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """RQ's monitor calls ``wait_for_horse`` a SECOND time after ``kill_horse``, as an
    unbounded final reap outside any death-penalty context. Self-raising there would
    escape ``monitor_work_horse`` and drop the job's failure handling — so once the
    current horse is killed, the poll blocks until it is reaped instead of timing out.
    """
    worker = worker_pools.CustomRQWorker.__new__(worker_pools.CustomRQWorker)
    worker.job_monitoring_interval = 0.02  # type: ignore[attr-defined]
    worker._horse_pid = 424242  # type: ignore[attr-defined]
    worker._killed_horse_pid = 0  # type: ignore[attr-defined]
    worker._report_progress = _no_report  # type: ignore[attr-defined]

    # Simulate rq's kill_horse recording the target, then a horse that only gets
    # reaped after several polls (longer than job_monitoring_interval).
    worker._killed_horse_pid = worker._horse_pid  # type: ignore[attr-defined]
    calls = {"n": 0}

    def fake_wait4(pid: int, flags: int) -> tuple[int, int, object]:
        calls["n"] += 1
        if calls["n"] < 4:
            return (0, 0, None)  # not reaped yet
        return (pid, 0, "rusage")

    monkeypatch.setattr(worker_pools.os, "wait4", fake_wait4)
    # It must NOT raise despite the interval elapsing across polls.
    assert worker.wait_for_horse() == (424242, 0, "rusage")


def test_kill_horse_marks_the_horse_it_kills_so_its_reap_waits_for_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``kill_horse`` records the target before rq signals it; the reap of that horse then polls
    past ``job_monitoring_interval`` until the horse is gone instead of timing itself out."""
    worker = worker_pools.CustomRQWorker.__new__(worker_pools.CustomRQWorker)
    worker.job_monitoring_interval = 0.02  # type: ignore[attr-defined]
    worker._horse_pid = 424242  # type: ignore[attr-defined]
    worker._killed_horse_pid = 0  # type: ignore[attr-defined]
    worker._report_progress = _no_report  # type: ignore[attr-defined]
    signalled: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(
        worker_pools.Worker,
        "kill_horse",
        lambda self, sig=signal.SIGKILL: signalled.append((self._killed_horse_pid, sig)),
    )

    worker.kill_horse()

    assert signalled == [(424242, signal.SIGKILL)], "rq signalled the horse before it was recorded"
    polls = {"n": 0}

    def fake_wait4(pid: int, flags: int) -> tuple[int, int, object]:
        polls["n"] += 1
        return (0, 0, None) if polls["n"] < 6 else (pid, 9, "rusage")

    monkeypatch.setattr(worker_pools, "_HORSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker_pools.os, "wait4", fake_wait4)

    assert worker.wait_for_horse() == (424242, 9, "rusage")


def test_prefork_horse_monitor_reads_an_already_reaped_horse_as_no_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """A horse that is no longer this process's child is reported as rq reports a vanished horse."""
    worker = worker_pools.CustomRQWorker.__new__(worker_pools.CustomRQWorker)
    worker.job_monitoring_interval = 5  # type: ignore[attr-defined]
    worker._horse_pid = 424242  # type: ignore[attr-defined]
    worker._killed_horse_pid = 0  # type: ignore[attr-defined]
    worker._report_progress = _no_report  # type: ignore[attr-defined]

    def _no_child(pid: int, flags: int) -> tuple[int, int, object]:
        raise ChildProcessError(10, "No child processes")

    monkeypatch.setattr(worker_pools.os, "wait4", _no_child)

    assert worker.wait_for_horse() == (None, None, None)


# --- the job budget at run time ----------------------------------------------


@pytest.mark.parametrize("stored_timeout", [300, None, 180])
def test_the_worker_applies_the_current_job_budget_to_every_job_it_runs(
    monkeypatch: pytest.MonkeyPatch, stored_timeout: int | None
) -> None:
    """A job carries the timeout it was stored with (an already-fired schedule keeps its first one);
    the worker replaces it with the budget it is configured with when it prepares the job, so a
    changed ``RQ_TASK_TIMEOUT`` takes effect on the next run once the worker restarts."""
    from tai42_kit.settings import reset_all_settings

    prepared: list[Any] = []

    class Base:
        def __init__(self, queues: Any, *args: Any, **kwargs: Any) -> None:
            self.name = kwargs["name"]

        def prepare_execution(self, job: Any) -> Any:
            prepared.append(job.timeout)
            return "execution"

    class TestWorker(worker_pools._TaiWorkerMixin, Base):
        pass

    job = SimpleNamespace(id="nightly", timeout=stored_timeout)
    monkeypatch.setenv("RQ_TASK_TIMEOUT", "60")
    reset_all_settings()
    try:
        assert TestWorker(["default"], name="w1", report_progress=_no_report).prepare_execution(job) == "execution"
    finally:
        monkeypatch.delenv("RQ_TASK_TIMEOUT", raising=False)
        reset_all_settings()
    assert job.timeout == 60
    assert prepared == [60]


def test_both_worker_classes_prepare_through_the_budget_seam():
    from rq import SimpleWorker, Worker

    for cls in (worker_pools.CustomRQWorker, worker_pools.CustomRQSimpleWorker):
        assert cls.prepare_execution is worker_pools._TaiWorkerMixin.prepare_execution
    assert Worker.prepare_execution is not worker_pools._TaiWorkerMixin.prepare_execution
    assert SimpleWorker.prepare_execution is not worker_pools._TaiWorkerMixin.prepare_execution


def test_importing_the_worker_loads_no_gevent() -> None:
    """Only ``--pool gevent`` loads gevent (its process prelude), never the import."""
    script = textwrap.dedent(
        """
        import sys

        sys.path.insert(0, sys.argv[1])
        import conftest  # noqa: F401

        from tai42_backend_rq import worker  # noqa: F401

        print(sorted(name for name in sys.modules if name.split(".")[0] == "gevent"))
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, os.path.dirname(__file__)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "[]"
