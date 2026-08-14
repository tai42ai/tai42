"""The worker runtime: fork safety, the fork gate, worker classes, and the worker
start wiring."""

from __future__ import annotations

import os
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from click.testing import CliRunner
from tai42_kit.fork_gate import fork_gate

from tai42_backend_rq import worker as worker_module
from tests.conftest import FakeRedisConn, FakeRqWorker

# --- worker classes ----------------------------------------------------------


def test_worker_classes_compose_rq_workers():
    from rq import SimpleWorker, Worker

    assert issubclass(worker_module.CustomRQWorker, Worker)
    assert issubclass(worker_module.CustomRQSimpleWorker, SimpleWorker)
    assert issubclass(worker_module.CustomRQWorker, worker_module._TaiWorkerMixin)
    assert issubclass(worker_module.CustomRQSimpleWorker, worker_module._TaiWorkerMixin)


def test_mixin_installs_signal_handlers_only_on_main_thread():
    installed: list[str] = []

    class Base:
        def __init__(self, queues: Any, *args: Any, **kwargs: Any) -> None:
            self.name = kwargs["name"]

        def _install_signal_handlers(self) -> None:
            installed.append("rq")

    class TestWorker(worker_module._TaiWorkerMixin, Base):
        pass

    worker = TestWorker(["default"], name="w1")
    worker._install_signal_handlers()
    assert installed == ["rq"]

    # Off the main thread the install is skipped (signal.signal would raise).
    thread = threading.Thread(target=worker._install_signal_handlers)
    thread.start()
    thread.join(timeout=5)
    assert installed == ["rq"]


def test_mixin_bounds_the_dequeue_poll():
    class Base:
        def __init__(self, queues: Any, *args: Any, **kwargs: Any) -> None:
            self.name = kwargs["name"]

    class TestWorker(worker_module._TaiWorkerMixin, Base):
        pass

    assert TestWorker(["default"], name="w1").dequeue_timeout == worker_module._DEQUEUE_POLL_SECONDS


# --- fork safety -------------------------------------------------------------


def test_after_fork_in_child_evicts_monitoring(app):
    worker_module._after_fork_in_child()
    assert app.monitoring.active.writer.shutdown_calls == 1


def test_after_fork_in_child_resets_the_inherited_fork_gate(app):
    """The fork happens INSIDE a job span, so the child inherits that span. Left in
    place it would block the child's own gate use against a hold only the parent can
    release."""
    # The child's inherited copy, as the fork leaves it: this span is open and only the
    # PARENT will ever close it. Set directly rather than through a real ``job_span``,
    # whose exit would then decrement the freshly-reset counter below zero.
    fork_gate._live_spans = 1
    try:
        worker_module._after_fork_in_child()
        assert fork_gate.live_spans == 0
        assert not fork_gate.blocked
    finally:
        fork_gate.reset_after_fork()


def test_install_fork_hooks_registers_once(monkeypatch):
    registered: list[dict[str, Any]] = []
    monkeypatch.setattr(worker_module, "_fork_hooks_installed", False)
    monkeypatch.setattr(os, "register_at_fork", lambda **kwargs: registered.append(kwargs))

    worker_module.install_fork_hooks()
    assert registered == [{"after_in_child": worker_module._after_fork_in_child}]

    # ``os.register_at_fork`` has no deregister, so a second call must not stack a
    # duplicate that would run the hook twice per child.
    worker_module.install_fork_hooks()
    assert len(registered) == 1


def test_prepare_forking_worker_evicts_without_registering_the_hook(app, monkeypatch):
    """The prefork-only prep drops the parent's monitoring client. The child hook is
    registered from the shared build seam instead — every pool needs it."""
    registered: list[dict[str, Any]] = []
    monkeypatch.setattr(os, "register_at_fork", lambda **kwargs: registered.append(kwargs))
    monkeypatch.setattr(worker_module.sys, "platform", "linux")

    worker_module.prepare_forking_worker()
    assert app.monitoring.active.writer.shutdown_calls == 1
    assert registered == []


def test_work_horse_perform_job_flushes_monitoring(app, monkeypatch):
    """The work-horse exits via os._exit, so the job path must flush buffered
    spans explicitly after the job ran."""
    ran: list[str] = []
    monkeypatch.setattr(worker_module.Worker, "perform_job", lambda self, job, queue: ran.append("job") or True)
    horse = object.__new__(worker_module.CustomRQWorker)

    assert horse.perform_job("job", "queue") is True
    assert ran == ["job"]
    assert app.monitoring.active.writer.flush_calls == 1


def test_work_horse_flushes_even_when_the_job_raises(app, monkeypatch):
    def boom(self: Any, job: Any, queue: Any) -> bool:
        raise RuntimeError("job blew up")

    monkeypatch.setattr(worker_module.Worker, "perform_job", boom)
    horse = object.__new__(worker_module.CustomRQWorker)

    with pytest.raises(RuntimeError, match="job blew up"):
        horse.perform_job("job", "queue")
    assert app.monitoring.active.writer.flush_calls == 1


def test_work_horse_flush_failure_is_logged_not_raised(app, monkeypatch, caplog):
    monkeypatch.setattr(worker_module.Worker, "perform_job", lambda self, job, queue: True)

    def failing_flush() -> None:
        raise RuntimeError("flush broke")

    monkeypatch.setattr(app.monitoring.active.writer, "flush", failing_flush)
    horse = object.__new__(worker_module.CustomRQWorker)

    with caplog.at_level("ERROR"):
        assert horse.perform_job("job", "queue") is True
    assert any("monitoring flush failed" in record.message for record in caplog.records)


# --- the fork gate: no job child alive during a manifest re-import -----------


def test_prefork_holds_the_span_around_the_fork_instant(monkeypatch):
    """The prefork pool gates ``fork_work_horse`` — the ``os.fork()`` call itself. The
    child inherits a SNAPSHOT of the import locks, so that instant is the whole
    exposure."""
    spans: list[int] = []
    monkeypatch.setattr(
        worker_module.Worker, "fork_work_horse", lambda self, job, queue: spans.append(fork_gate.live_spans)
    )
    worker = object.__new__(worker_module.CustomRQWorker)

    worker.fork_work_horse("job", "queue")
    assert spans == [1]
    assert fork_gate.live_spans == 0
    # The custom override still does its own bookkeeping.
    assert worker._killed_horse_pid == 0


def test_prefork_does_not_hold_the_span_across_the_horses_whole_run(monkeypatch):
    """The span must NOT cover fork→reap: a horse already forked is immune to later
    re-imports, so holding the gate for its whole job would block reloads behind
    arbitrarily long jobs for no protection."""
    during_monitor: list[int] = []
    monkeypatch.setattr(worker_module.Worker, "fork_work_horse", lambda self, job, queue: None)
    monkeypatch.setattr(
        worker_module.Worker, "monitor_work_horse", lambda self, job, queue: during_monitor.append(fork_gate.live_spans)
    )
    # rq's execute_job is prepare -> fork -> monitor -> set_state; the two that touch
    # Redis are stubbed so the ordering assertion needs no live connection.
    monkeypatch.setattr(worker_module.Worker, "prepare_execution", lambda self, job: None)
    monkeypatch.setattr(worker_module.Worker, "set_state", lambda self, state, pipeline=None: None)
    worker = cast("Any", object.__new__(worker_module.CustomRQWorker))

    # rq's own execute_job runs unwrapped: only the fork instant inside it is gated.
    worker.execute_job("job", "queue")
    assert during_monitor == [0], "the gate is still held while the horse runs"
    assert fork_gate.live_spans == 0


def test_prefork_releases_its_span_when_the_fork_raises(monkeypatch):
    def boom(self: Any, job: Any, queue: Any) -> None:
        raise RuntimeError("fork blew up")

    monkeypatch.setattr(worker_module.Worker, "fork_work_horse", boom)
    worker = object.__new__(worker_module.CustomRQWorker)

    with pytest.raises(RuntimeError, match="fork blew up"):
        worker.fork_work_horse("job", "queue")
    assert fork_gate.live_spans == 0


def test_simple_pool_holds_the_span_across_the_whole_in_process_job(monkeypatch):
    """The non-forking pools have no child and no inherited snapshot: the job body
    imports against live ``sys.modules`` for its whole duration, so the span covers it
    all. It must NOT route through the prefork fork seam."""
    spans: list[int] = []
    monkeypatch.setattr(
        worker_module.CustomRQSimpleWorker,
        "_run_job_inline",
        lambda self, job, queue: spans.append(fork_gate.live_spans),
    )
    worker = object.__new__(worker_module.CustomRQSimpleWorker)

    worker.execute_job("job", "queue")
    assert spans == [1]
    assert fork_gate.live_spans == 0
    assert not hasattr(worker, "fork_work_horse")


def test_simple_pool_releases_its_span_when_the_job_body_raises(monkeypatch):
    def boom(self: Any, job: Any, queue: Any) -> None:
        raise RuntimeError("job blew up")

    monkeypatch.setattr(worker_module.CustomRQSimpleWorker, "_run_job_inline", boom)
    worker = object.__new__(worker_module.CustomRQSimpleWorker)

    with pytest.raises(RuntimeError, match="job blew up"):
        worker.execute_job("job", "queue")
    assert fork_gate.live_spans == 0


def test_start_scheduler_runs_inside_a_fork_gate_span(monkeypatch):
    """``work()`` spawns the scheduler process before its dequeue loop — the worker's
    FIRST child, and a spawn like any other."""
    spans: list[int] = []
    monkeypatch.setattr(
        worker_module.Worker, "_start_scheduler", lambda self, *a, **kw: spans.append(fork_gate.live_spans)
    )
    worker = object.__new__(worker_module.CustomRQWorker)

    worker._start_scheduler(False, "INFO")
    assert spans == [1]
    assert fork_gate.live_spans == 0


def test_run_maintenance_tasks_runs_inside_a_fork_gate_span(monkeypatch):
    """rq's maintenance pass re-spawns a dead scheduler process — the same hazard as
    a work-horse fork, so it takes the same span."""
    spans: list[int] = []
    monkeypatch.setattr(worker_module.Worker, "run_maintenance_tasks", lambda self: spans.append(fork_gate.live_spans))
    worker = object.__new__(worker_module.CustomRQWorker)

    worker.run_maintenance_tasks()
    assert spans == [1]
    assert fork_gate.live_spans == 0


def test_prepare_forking_worker_darwin_disables_proxy_detection(app, monkeypatch):
    import urllib.request

    monkeypatch.setattr(worker_module.sys, "platform", "darwin")
    monkeypatch.setenv("no_proxy", "original")
    monkeypatch.setattr(urllib.request, "getproxies", urllib.request.getproxies)

    worker_module.prepare_forking_worker()
    assert os.environ["no_proxy"] == "*"
    assert urllib.request.getproxies() == {}


# --- worker build + run paths -------------------------------------------------


class FakeQueue:
    def __init__(self, connection: Any = None) -> None:
        self.connection = connection


def _fake_worker_class(record: dict[str, Any]) -> type:
    class FakeWorker:
        name = "fake"

        def __init__(self, queues: Any, name: str | None, connection: Any, default_result_ttl: int) -> None:
            record["queues"] = queues
            record["name"] = name
            record["connection"] = connection
            record["default_result_ttl"] = default_result_ttl
            self.name = name

        def work(self, **kwargs: Any) -> None:
            record["work"] = kwargs

    return FakeWorker


@pytest.fixture
def worker_env(monkeypatch) -> dict[str, Any]:
    record: dict[str, Any] = {"prepared": 0, "gevent": 0, "hooks": 0}
    conn = FakeRedisConn()
    record["conn"] = conn
    monkeypatch.setattr(worker_module, "Redis", type("R", (), {"from_url": staticmethod(lambda url: conn)}))
    monkeypatch.setattr(worker_module, "Queue", FakeQueue)
    monkeypatch.setattr(
        worker_module, "prepare_forking_worker", lambda: record.__setitem__("prepared", record["prepared"] + 1)
    )
    monkeypatch.setattr(worker_module, "install_fork_hooks", lambda: record.__setitem__("hooks", record["hooks"] + 1))
    monkeypatch.setattr(worker_module, "setup_gevent", lambda: record.__setitem__("gevent", record["gevent"] + 1))
    fake_cls = _fake_worker_class(record)
    monkeypatch.setattr(worker_module, "CustomRQWorker", fake_cls)
    monkeypatch.setattr(worker_module, "CustomRQSimpleWorker", fake_cls)
    return record


def test_start_rq_worker_prefork(worker_env):
    worker_module.start_rq_worker(None, "w1", "info", False, 500, "prefork")
    assert worker_env["prepared"] == 1
    assert worker_env["gevent"] == 0
    assert worker_env["hooks"] == 1
    assert worker_env["name"] == "w1"
    # --results-ttl reaches rq as the worker's default result TTL.
    assert worker_env["default_result_ttl"] == 500
    assert worker_env["work"] == {"burst": False, "logging_level": "INFO", "with_scheduler": True}
    assert worker_env["conn"].closed


def test_start_rq_worker_solo(worker_env):
    worker_module.start_rq_worker("redis://custom", "w2", "debug", True, 100, "solo")
    assert worker_env["prepared"] == 0
    # The non-forking pools still spawn the rq-scheduler child, so they need the hook.
    assert worker_env["hooks"] == 1
    assert worker_env["default_result_ttl"] == 100
    assert worker_env["work"] == {"burst": True, "logging_level": "DEBUG", "with_scheduler": True}


def test_start_rq_worker_gevent(worker_env):
    worker_module.start_rq_worker(None, "w3", "info", False, 500, "gevent")
    assert worker_env["gevent"] == 1
    assert worker_env["prepared"] == 0
    assert worker_env["hooks"] == 1


def test_main_cli_invokes_start(monkeypatch):
    calls: list[Any] = []
    monkeypatch.setattr(worker_module, "start_rq_worker", lambda *args: calls.append(args))
    worker_module.main.callback(None, "w1", "INFO", False, 500, "solo")  # type: ignore[misc]
    assert calls == [(None, "w1", "INFO", False, 500, "solo")]


def test_main_cli_without_name_leaves_the_worker_name_to_rq(monkeypatch):
    """Invoked with no ``--name``, the CLI resolves the name to None and passes
    None down, so RQ auto-generates a unique name per worker process. A fixed
    default would make RQ's ``register_birth`` reject a second worker under the
    same name — no multi-worker fleet, and no restart of a killed worker while
    its registry entry is still around."""
    calls: list[Any] = []
    monkeypatch.setattr(worker_module, "start_rq_worker", lambda *args: calls.append(args))

    result = CliRunner().invoke(worker_module.main, [])

    assert result.exit_code == 0, result.output
    assert calls == [(None, None, "INFO", False, 500, "prefork")]


def test_build_worker_hands_a_none_name_to_the_worker_class(worker_env):
    # The None reaches the rq worker class as ``name=None`` — that is what makes
    # rq generate the unique name; anything defaulted along the way would not.
    worker_module.start_rq_worker(None, None, "info", False, 500, "solo")

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
    }


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

    assert calls == [("redis://x/1", None, 500, "gevent")]
    assert runtime._worker is worker
    assert runtime._redis_conn is conn


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

    class IdleWorker(worker_module._TaiWorkerMixin, Base):
        pass

    worker = IdleWorker()
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
    assert worker.polls[0] == (worker_module._DEQUEUE_POLL_SECONDS, worker_module._DEQUEUE_POLL_SECONDS)


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
    worker = worker_module.CustomRQWorker.__new__(worker_module.CustomRQWorker)
    worker.job_monitoring_interval = 0.05  # type: ignore[attr-defined]
    worker._horse_pid = 424242  # type: ignore[attr-defined]
    worker._killed_horse_pid = 0  # type: ignore[attr-defined]

    # A horse that has not exited: WNOHANG keeps reporting "not reaped yet".
    monkeypatch.setattr(worker_module.os, "wait4", lambda pid, flags: (0, 0, None))
    started = time.monotonic()
    with pytest.raises(worker_module.HorseMonitorTimeoutException):
        worker.wait_for_horse()
    # It timed itself out on real time, promptly — not after the job ended.
    assert time.monotonic() - started < 1.0


def test_prefork_horse_monitor_returns_the_reaped_horse(monkeypatch: pytest.MonkeyPatch) -> None:
    worker = worker_module.CustomRQWorker.__new__(worker_module.CustomRQWorker)
    worker.job_monitoring_interval = 5  # type: ignore[attr-defined]
    worker._horse_pid = 424242  # type: ignore[attr-defined]
    worker._killed_horse_pid = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(worker_module.os, "wait4", lambda pid, flags: (424242, 0, "rusage"))

    assert worker.wait_for_horse() == (424242, 0, "rusage")


def test_work_horse_child_restores_the_signal_death_penalty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only SIGALRM can interrupt job code blocked in a C call, and the forking
    thread becomes the child's main thread — so the horse enforces job timeouts with
    RQ's signal penalty even though the parent cannot arm one."""
    worker = worker_module.CustomRQWorker.__new__(worker_module.CustomRQWorker)
    monkeypatch.setattr(worker_module.Worker, "main_work_horse", lambda self, job, queue: None)

    worker.main_work_horse(object(), object())

    assert worker.death_penalty_class is worker_module.UnixSignalDeathPenalty


def test_simple_worker_refreshes_heartbeats_while_a_job_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The non-forking worker has no monitor process, so nothing would refresh its
    heartbeats for the job's whole duration — a long job would read as a dead worker.
    Its heartbeat thread is what keeps both pools on ONE liveness contract."""
    worker = worker_module.CustomRQSimpleWorker.__new__(worker_module.CustomRQSimpleWorker)
    spy = _HeartbeatSpy()
    worker.job_monitoring_interval = spy.job_monitoring_interval  # type: ignore[attr-defined]
    monkeypatch.setattr(type(worker), "maintain_heartbeats", lambda self, job: spy.maintain_heartbeats(job))
    monkeypatch.setattr(type(worker), "prepare_execution", lambda self, job: None)
    monkeypatch.setattr(type(worker), "set_state", lambda self, state: None)
    monkeypatch.setattr(type(worker), "perform_job", lambda self, job, queue: time.sleep(0.2) or True)

    worker.execute_job(SimpleNamespace(id="job-1"), object())

    assert spy.beats >= 2, "a busy non-forking worker never refreshed its heartbeat"


def test_wait_for_horse_after_kill_does_not_self_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """RQ's monitor calls ``wait_for_horse`` a SECOND time after ``kill_horse``, as an
    unbounded final reap outside any death-penalty context. Self-raising there would
    escape ``monitor_work_horse`` and drop the job's failure handling — so once the
    current horse is killed, the poll blocks until it is reaped instead of timing out.
    """
    worker = worker_module.CustomRQWorker.__new__(worker_module.CustomRQWorker)
    worker.job_monitoring_interval = 0.02  # type: ignore[attr-defined]
    worker._horse_pid = 424242  # type: ignore[attr-defined]
    worker._killed_horse_pid = 0  # type: ignore[attr-defined]

    # Simulate rq's kill_horse recording the target, then a horse that only gets
    # reaped after several polls (longer than job_monitoring_interval).
    worker._killed_horse_pid = worker._horse_pid  # type: ignore[attr-defined]
    calls = {"n": 0}

    def fake_wait4(pid: int, flags: int) -> tuple[int, int, object]:
        calls["n"] += 1
        if calls["n"] < 4:
            return (0, 0, None)  # not reaped yet
        return (pid, 0, "rusage")

    monkeypatch.setattr(worker_module.os, "wait4", fake_wait4)
    # It must NOT raise despite the interval elapsing across polls.
    assert worker.wait_for_horse() == (424242, 0, "rusage")
