"""The worker runtime: fork safety, the fork gate, worker classes, and the worker
start wiring."""

from __future__ import annotations

import asyncio
import os
import signal
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import pytest
from click.testing import CliRunner
from tai42_kit.fork_gate import fork_gate

from tai42_backend_rq import worker as worker_module

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


class FakeRedisConn:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


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


# --- warm shutdown -------------------------------------------------------------


def test_request_warm_shutdown_busy_worker_defers_to_rq():
    class BusyWorker:
        name = "w1"

        def __init__(self) -> None:
            self.stops: list[Any] = []
            self._stop_requested = False

        def request_stop(self, signum: Any, frame: Any) -> None:
            # rq's busy path: sets its own stop flag, no exception.
            self.stops.append(signum)
            self._stop_requested = True

    worker = BusyWorker()
    worker_module.request_warm_shutdown(worker)
    assert worker.stops == [signal.SIGTERM]
    assert worker._stop_requested is True


def test_request_warm_shutdown_idle_worker_sets_stop_flag():
    from rq.exceptions import StopRequested

    class IdleWorker:
        name = "w1"

        def __init__(self) -> None:
            self._stop_requested = False

        def request_stop(self, signum: Any, frame: Any) -> None:
            # rq's idle path: raises to unwind the work loop it does not run on.
            raise StopRequested

    worker = IdleWorker()
    worker_module.request_warm_shutdown(worker)
    assert worker._stop_requested is True


def test_request_warm_shutdown_off_main_thread_sets_flag_directly():
    """Off the main thread rq's ``request_stop`` cannot run — its signal
    re-bind (``signal.signal``) is main-thread-only and raises ValueError — so
    the stop flag must be set directly, never through ``request_stop``."""

    class MainThreadOnlyWorker:
        name = "w1"

        def __init__(self) -> None:
            self._stop_requested = False

        def request_stop(self, signum: Any, frame: Any) -> None:
            # Mirrors rq: request_stop starts by re-binding process signal
            # handlers, which raises off the main thread.
            raise ValueError("signal only works in main thread of the main interpreter")

    worker = MainThreadOnlyWorker()
    thread = threading.Thread(target=worker_module.request_warm_shutdown, args=(worker,))
    thread.start()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert worker._stop_requested is True


# --- run_rq_worker (the launch path) -------------------------------------------


class _ThreadedFakeWorker:
    """Runs a stop-flag poll loop, like the real work loop off the main thread."""

    name = "w1"

    def __init__(self) -> None:
        self._stop_requested = False
        self.work_kwargs: dict[str, Any] | None = None
        self.work_thread: threading.Thread | None = None

    def request_stop(self, signum: Any, frame: Any) -> None:
        from rq.exceptions import StopRequested

        raise StopRequested

    def work(self, **kwargs: Any) -> None:
        self.work_kwargs = kwargs
        self.work_thread = threading.current_thread()
        while not self._stop_requested:
            time.sleep(0.005)


async def test_run_rq_worker_keeps_the_loop_alive_and_stops_warm_on_cancel(monkeypatch):
    worker = _ThreadedFakeWorker()
    conn = FakeRedisConn()
    monkeypatch.setattr(worker_module, "_build_worker", lambda *args: (worker, conn))

    task = asyncio.create_task(worker_module.run_rq_worker(None, "w1", "info", False, 500, "solo"))
    # The loop stays responsive while the work loop runs on a worker thread.
    async with asyncio.timeout(5):
        while worker.work_thread is None:
            await asyncio.sleep(0.005)
    assert worker.work_thread is not threading.main_thread()
    assert not task.done()
    assert worker.work_kwargs == {"burst": False, "logging_level": "INFO", "with_scheduler": True}

    # Cancellation requests a warm shutdown and awaits the work loop's exit.
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert worker._stop_requested is True
    assert conn.closed


async def test_run_rq_worker_returns_when_the_work_loop_exits(monkeypatch):
    class BurstWorker:
        name = "w1"

        def work(self, **kwargs: Any) -> None:
            pass

    conn = FakeRedisConn()
    monkeypatch.setattr(worker_module, "_build_worker", lambda *args: (BurstWorker(), conn))

    await worker_module.run_rq_worker(None, "w1", "info", True, 500, "solo")
    assert conn.closed


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
