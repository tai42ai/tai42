"""RqBackend: the launch dispatch to worker/beat/dashboard, and what the shared
lifecycle does around each of them.

The dispatch, the readiness gate and the shutdown wiring are the base's and are
proven in kit; what is asserted here is the RQ binding hanging off them — the
subcommand map, the argv the two inline runtimes publish, and the worker
runtime's off-loop run and warm drain seen end to end through ``launch``.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any

import pytest
from tai42_contract.backend.runtime import ExecutionMode

from tai42_backend_rq import worker as worker_module
from tai42_backend_rq.backend import RqBackend, RqBeatRuntime, RqDashboardRuntime

from .conftest import FakeRedisConn, FakeRqWorker

# The registration decorator's static type is a union (decorator-or-class);
# at runtime the stub app returns the class unchanged.
_RqBackendCls: Any = RqBackend


@pytest.fixture
def backend() -> Any:
    return _RqBackendCls()


# --- dispatch -----------------------------------------------------------------


def test_the_backend_declares_the_three_rq_runtimes():
    assert _RqBackendCls.label == "rq"
    assert dict(_RqBackendCls.runtimes) == {
        "worker": worker_module.RqWorkerRuntime,
        "beat": RqBeatRuntime,
        "dashboard": RqDashboardRuntime,
    }


async def test_launch_requires_a_subcommand(backend):
    with pytest.raises(ValueError, match="rq launch requires a subcommand: worker, beat, dashboard"):
        await backend.launch([])


async def test_launch_unknown_subcommand_names_the_declared_runtimes(backend):
    with pytest.raises(ValueError, match="Unknown rq launch subcommand 'flower'"):
        await backend.launch(["flower"])


async def test_launch_worker_rejects_unknown_options(backend):
    """Parsed before anything starts: an unknown option never reaches a worker."""
    import click

    with pytest.raises(click.UsageError):
        await backend.launch(["worker", "--frobnicate"])


# --- the inline runtimes (beat / dashboard) -----------------------------------


async def test_launch_beat_runs_the_scheduler_cli(backend, monkeypatch):
    import rq_scheduler.scripts.rqscheduler as rqscheduler_script

    seen_argv: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(rqscheduler_script, "main", lambda: seen_argv.append(list(sys.argv)))

    await backend.launch(["beat", "--interval", "5"])
    assert seen_argv == [["rq-scheduler", "--interval", "5"]]


async def test_launch_dashboard_runs_the_dashboard_cli(backend, monkeypatch):
    import rq_dashboard.cli as dashboard_cli

    seen_argv: list[list[str]] = []
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(dashboard_cli, "run", lambda: seen_argv.append(list(sys.argv)))

    await backend.launch(["dashboard", "--port", "9181"])
    assert seen_argv == [["rq-dashboard", "--port", "9181"]]


async def test_the_inline_runtimes_block_the_loop_on_purpose(backend, monkeypatch):
    """Beat and the dashboard pull no work and nothing else runs on their loop,
    so they run ON it and keep their own vendor signal disposition — the base
    wires them nothing."""
    import rq_scheduler.scripts.rqscheduler as rqscheduler_script

    threads: list[threading.Thread] = []
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(rqscheduler_script, "main", lambda: threads.append(threading.current_thread()))

    for runtime_cls in (RqBeatRuntime, RqDashboardRuntime):
        assert runtime_cls.mode is ExecutionMode.inline
        assert runtime_cls.consumes_work is False

    await backend.launch(["beat"])
    assert threads == [threading.main_thread()]


# --- the worker runtime, end to end through launch -----------------------------


@pytest.fixture
def launched_worker(monkeypatch) -> tuple[Any, Any]:
    """The engine objects ``build`` hands back, with the real construction (and
    its Redis connection) stubbed out."""
    worker = FakeRqWorker()
    conn = FakeRedisConn()
    monkeypatch.setattr(worker_module, "_build_worker", lambda *args: (worker, conn))
    return worker, conn


async def test_launch_worker_runs_the_work_loop_on_a_daemon_thread(backend, launched_worker):
    """The blocking loop must leave the serving loop free: the app's worker-bus
    subscription lives there, and a starved loop delivers no fleet op."""
    worker, _conn = launched_worker

    task = asyncio.create_task(backend.launch(["worker", "--pool", "solo"]))
    async with asyncio.timeout(5):
        while worker.work_thread is None:
            await asyncio.sleep(0.005)

    assert worker.work_thread is not threading.main_thread()
    # Daemon, so a wedged engine cannot hold the process open past teardown.
    assert worker.work_thread.daemon
    assert worker.work_thread.name == "tai-backend-rq-worker"
    assert not task.done()
    assert worker.work_kwargs == {"burst": False, "logging_level": "INFO", "with_scheduler": True}

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_launch_worker_drains_warm_on_cancellation(backend, launched_worker):
    """A recycle cancels the launch; the base then requests the drain and awaits
    the work loop's own exit before the cancellation is re-raised — and the
    drain never goes through rq's signal-rebinding ``request_stop``."""
    worker, conn = launched_worker

    task = asyncio.create_task(backend.launch(["worker", "--pool", "solo"]))
    async with asyncio.timeout(5):
        while worker.work_thread is None:
            await asyncio.sleep(0.005)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert worker._stop_requested is True
    assert worker.stop_requests == []
    assert conn.closed


async def test_launch_worker_returns_when_the_work_loop_exits(backend, launched_worker):
    """Burst mode: the loop returns on its own, and the connection is released
    on that path too."""
    worker, conn = launched_worker
    worker._stop_requested = True

    await backend.launch(["worker", "--pool", "solo", "--burst"])

    assert worker.work_kwargs == {"burst": True, "logging_level": "INFO", "with_scheduler": True}
    assert conn.closed


def test_the_drain_budget_tracks_the_live_task_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A warm drain waits for the running job, so the budget has to outlast the job
    budget — read LIVE, so a settings epoch that widens ``RQ_TASK_TIMEOUT`` widens
    the drain with it instead of abandoning a work-horse it should have waited for."""
    from tai42_kit.settings import reset_all_settings

    from tai42_backend_rq.settings import rq_settings

    instance = _RqBackendCls()
    assert instance.drain_timeout == float(rq_settings().task_timeout) + 30.0

    monkeypatch.setenv("RQ_TASK_TIMEOUT", "900")
    reset_all_settings()
    try:
        assert instance.drain_timeout == 930.0
    finally:
        monkeypatch.delenv("RQ_TASK_TIMEOUT", raising=False)
        reset_all_settings()
