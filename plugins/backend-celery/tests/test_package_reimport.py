"""A config reload re-imports this plugin's whole package between a runtime's build and its run.

The host re-imports the package named as its ``backend_module`` on every epoch build, the
first-connect resync before the worker's boot-ready latch included, so the package's import-time
registrations re-fire into each epoch. Each test here builds a runtime, re-imports the package the
way the host does (every module of it popped from ``sys.modules``, then imported again), and then
runs the runtime: it runs on what its build made. The re-imported copies it reaches are the classes
celery takes only by import path, which keep no state of their own.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import pkgutil
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import click
import pytest
from celery import _state as celery_state
from celery import signals
from celery.bin.celery import celery as celery_command
from celery.concurrency import solo
from celery.utils.dispatch import Signal
from tai42_kit.clients.impl.redis_connections import KitSyncConnectionPool
from tai42_kit.fork_gate import fork_gate

import tai42_backend_celery
from tai42_backend_celery.core import embedded_beat, liveness_step
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.backend import CeleryBeatRuntime, CeleryFlowerRuntime, CeleryWorkerRuntime
from tai42_backend_celery.core.redis_seam import KitRedisBackend

_PACKAGE = tai42_backend_celery.__name__


class _Reports:
    def __init__(self) -> None:
        self.promises: list[float] = []

    def __call__(self, *, next_within: float) -> None:
        self.promises.append(next_within)


class _Timer:
    """The consumer's timer, keeping each repeating entry's function."""

    def __init__(self) -> None:
        self.funs: list[Callable[[], None]] = []

    def call_repeatedly(self, secs: float, fun: Callable[[], None]) -> SimpleNamespace:
        self.funs.append(fun)
        return SimpleNamespace(cancel=lambda: None)


class _Beat:
    """An embedded beat child that is still running until it is terminated."""

    pid = 4242

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._alive = True

    def is_alive(self) -> bool:
        return self._alive

    def terminate(self) -> None:
        self.calls.append("terminate")
        self._alive = False

    def join(self, timeout: float | None = None) -> None:
        self.calls.append("join")


class _StoppingWorker:
    """The worker celery sends ``worker_shutdown`` from, holding the app it ran."""

    def __init__(self, app: Any) -> None:
        self.app = app


def _green_pool() -> Any:
    """A pool of celery's gevent module, whose jobs are green threads, built without importing gevent."""
    return type("TaskPool", (), {"__module__": "celery.concurrency.gevent", "is_green": True})()


def _package_module_names() -> list[str]:
    return [_PACKAGE, *(info.name for info in pkgutil.walk_packages(tai42_backend_celery.__path__, f"{_PACKAGE}."))]


def _celery_signals() -> list[Signal]:
    return [value for value in vars(signals).values() if isinstance(value, Signal)]


@pytest.fixture
def reimport_package(stub_app: Any) -> Iterator[Callable[[], ModuleType]]:
    """Re-import the plugin's package as the host's reload does; the test process gets its own copies back after.

    The re-import registers into the stub app and connects to celery's process-global signals, as
    it does in a host; what it registered and the receivers it connected are undone with
    ``sys.modules``, and the app it made current by the suite's per-test restore.
    """
    names = _package_module_names()
    kept_modules = {name: module for name, module in sys.modules.items() if name.split(".")[0] == _PACKAGE}
    kept_receivers = {signal: list(signal.receivers or []) for signal in _celery_signals()}
    kept_finalizers = set(celery_state._on_app_finalizers)
    kept_registrations = (
        dict(stub_app.tools.registered),
        dict(stub_app.tools.tags),
        dict(stub_app.extensions.registered),
        stub_app.backends.registered_cls,
        stub_app.backends.instance,
        list(stub_app.lifecycle.fleet_op_applied),
    )

    def _reimport() -> ModuleType:
        for name in [name for name in sys.modules if name.split(".")[0] == _PACKAGE]:
            del sys.modules[name]
        for name in names:
            importlib.import_module(name)
        fresh = sys.modules[_PACKAGE]
        assert fresh is not tai42_backend_celery
        return fresh

    yield _reimport

    for name in [name for name in sys.modules if name.split(".")[0] == _PACKAGE]:
        del sys.modules[name]
    sys.modules.update(kept_modules)
    for signal, receivers in kept_receivers.items():
        signal.receivers = receivers
        signal.sender_receivers_cache.clear()
    celery_state._on_app_finalizers.clear()
    celery_state._on_app_finalizers.update(kept_finalizers)
    (
        stub_app.tools.registered,
        stub_app.tools.tags,
        stub_app.extensions.registered,
        stub_app.backends.registered_cls,
        stub_app.backends.instance,
        stub_app.lifecycle.fleet_op_applied,
    ) = kept_registrations


def _run_command(
    monkeypatch: pytest.MonkeyPatch, name: str, run: Callable[[Any], None], start: Callable[[], None]
) -> Any:
    """Run ``start`` with celery's ``name`` command replaced by one that calls ``run`` with the app the CLI runs.

    Returns that app; a raise inside ``run`` propagates.
    """
    seen: list[Any] = []

    @click.command(name, context_settings={"ignore_unknown_options": True, "allow_extra_args": True})
    @click.pass_context
    def _recorder(ctx: click.Context) -> None:
        seen.append(ctx.obj.app)
        assert ctx.obj.app is celery_app, "the celery CLI runs an app the re-import made, not the one built"
        run(ctx.obj.app)

    monkeypatch.setitem(celery_command.commands, name, _recorder)
    with pytest.raises(SystemExit) as exited:
        start()
    assert exited.value.code == 0
    [app] = seen
    return app


async def _built_worker(report: _Reports) -> CeleryWorkerRuntime:
    runtime = CeleryWorkerRuntime.from_args([])
    runtime.bind_progress(report)
    await runtime.build()
    return runtime


async def test_the_worker_runs_the_app_its_build_made_with_its_steps_and_reporter(
    monkeypatch: pytest.MonkeyPatch, reimport_package: Callable[[], ModuleType]
) -> None:
    report = _Reports()
    runtime = await _built_worker(report)
    reimport_package()
    timer = _Timer()

    def _start_the_consumer(app: Any) -> None:
        [step_cls] = [step for step in app.steps["consumer"] if step.__name__ == "ProgressStep"]
        consumer = SimpleNamespace(app=app, pool=solo.TaskPool(limit=1), timer=timer)
        step_cls(consumer).start(consumer)

    app = await asyncio.to_thread(_run_command, monkeypatch, "worker", _start_the_consumer, runtime.run_blocking)

    assert app is celery_app
    assert liveness_step.ProgressStep in app.steps["consumer"]
    assert embedded_beat.BeatWatchStep in app.steps["worker"]
    [tick] = timer.funs
    tick()
    assert report.promises == [1.0]


async def test_a_green_job_after_a_reload_runs_on_the_host_loop_the_build_handed(
    monkeypatch: pytest.MonkeyPatch, reimport_package: Callable[[], ModuleType]
) -> None:
    runtime = await _built_worker(_Reports())
    reimport_package()
    job_loops: list[asyncio.AbstractEventLoop] = []

    async def _loop_of_the_job() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    def _run_a_green_job(app: Any) -> None:
        [step_cls] = [step for step in app.steps["consumer"] if step.__name__ == "ProgressStep"]
        consumer = SimpleNamespace(app=app, pool=_green_pool(), timer=_Timer())
        step_cls(consumer).start(consumer)
        task = app.tasks["celery.tool_execution"]
        task.push_request(id="green-after-reload")
        try:
            job_loops.append(task.run_async(_loop_of_the_job()))
        finally:
            task.pop_request()

    await asyncio.to_thread(_run_command, monkeypatch, "worker", _run_a_green_job, runtime.run_blocking)

    assert job_loops == [asyncio.get_running_loop()]


async def test_the_worker_s_shutdown_after_a_reload_closes_the_loops_its_jobs_ran_on_once(
    stub_app: Any, monkeypatch: pytest.MonkeyPatch, reimport_package: Callable[[], ModuleType]
) -> None:
    runtime = await _built_worker(_Reports())
    reimport_package()
    job_loops: list[asyncio.AbstractEventLoop] = []

    async def _loop_of_the_job() -> asyncio.AbstractEventLoop:
        return asyncio.get_running_loop()

    def _run_jobs_then_stop(app: Any) -> None:
        [step_cls] = [step for step in app.steps["consumer"] if step.__name__ == "ProgressStep"]
        consumer = SimpleNamespace(app=app, pool=solo.TaskPool(limit=1), timer=_Timer())
        step_cls(consumer).start(consumer)
        for name in ("celery.tool_execution", "celery.callback_task"):
            task = app.tasks[name]
            task.push_request(id=f"{name}-after-reload")
            try:
                job_loops.append(task.run_async(_loop_of_the_job()))
            finally:
                task.pop_request()
        signals.worker_shutdown.send(sender=_StoppingWorker(app))

    await asyncio.to_thread(_run_command, monkeypatch, "worker", _run_jobs_then_stop, runtime.run_blocking)

    assert [loop.is_closed() for loop in job_loops] == [True, True]
    stub_app.monitoring.active.writer.flush.assert_called_once_with()


def _fork_an_exiting_child() -> None:
    pid = os.fork()
    if pid == 0:  # pragma: no cover - runs in the child
        os._exit(0)
    os.waitpid(pid, 0)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="os.fork is not available on this platform")
async def test_a_fork_waiting_on_the_hub_after_a_reload_reports_through_the_built_reporter(
    monkeypatch: pytest.MonkeyPatch, reimport_package: Callable[[], ModuleType]
) -> None:
    report = _Reports()
    runtime = await _built_worker(report)
    reimport_package()
    held, release = threading.Event(), threading.Event()

    def _rebuild() -> None:
        with fork_gate.exclusive(timeout=5.0):
            held.set()
            release.wait(10.0)

    def _fork_during_a_rebuild(app: Any) -> None:
        rebuilder = threading.Thread(target=_rebuild, daemon=True)
        rebuilder.start()
        assert held.wait(5.0)
        threading.Timer(1.5, release.set).start()
        _fork_an_exiting_child()
        rebuilder.join(10.0)

    await asyncio.to_thread(_run_command, monkeypatch, "worker", _fork_during_a_rebuild, runtime.run_blocking)

    assert report.promises
    assert set(report.promises) == {1.0}


async def test_the_worker_s_close_after_a_reload_reaps_the_beat_its_build_watched(
    liveness_path: Path, reimport_package: Callable[[], ModuleType]
) -> None:
    runtime = await _built_worker(_Reports())
    beat = _Beat()
    parent = SimpleNamespace(app=celery_app, beat=beat)
    embedded_beat.BeatWatchStep(parent).create(parent)
    assert celery_app.beat_reporter is not None
    celery_app.beat_reporter(next_within=5.0)
    beat_file = liveness_path.with_name(liveness_path.name + "-beat")
    assert beat_file.exists()
    reimport_package()

    await runtime.aclose()

    assert beat.calls == ["terminate", "join"]
    assert not beat_file.exists()


async def test_the_beat_runs_the_app_its_build_handed_its_reporter(
    monkeypatch: pytest.MonkeyPatch, reimport_package: Callable[[], ModuleType]
) -> None:
    runtime = CeleryBeatRuntime.from_args([])
    report = _Reports()
    runtime.bind_progress(report)
    await runtime.build()
    reimport_package()

    app = _run_command(monkeypatch, "beat", lambda app: None, runtime.run_blocking)

    assert app is celery_app
    assert app.beat_reporter == runtime.report_progress


def test_flower_runs_the_app_its_runtime_was_made_with(
    monkeypatch: pytest.MonkeyPatch, reimport_package: Callable[[], ModuleType]
) -> None:
    runtime = CeleryFlowerRuntime.from_args([])
    reimport_package()

    app = _run_command(monkeypatch, "flower", lambda app: None, runtime.run_blocking)

    assert app is celery_app


def test_a_thread_after_a_reload_resolves_the_result_backend_class_by_name_and_it_carries_the_kit_seam(
    reimport_package: Callable[[], ModuleType],
) -> None:
    """celery's loader names the Redis result backend class by import path, and celery builds a backend per thread.

    A thread that first uses the result backend after a reload therefore gets the re-imported class,
    which keeps no state of its own: it opens the kit's connection pool as the built class does.
    """
    reimport_package()
    backends: list[Any] = []
    thread = threading.Thread(target=lambda: backends.append(celery_app.backend))
    thread.start()
    thread.join(10.0)

    [backend] = backends
    assert type(backend) is sys.modules["tai42_backend_celery.core.redis_seam"].KitRedisBackend
    assert type(backend) is not KitRedisBackend
    assert type(backend.client.connection_pool) is KitSyncConnectionPool


def test_the_re_import_makes_fresh_copies_of_the_package(reimport_package: Callable[[], ModuleType]) -> None:
    fresh = reimport_package()

    assert fresh.core.app.celery_app is not celery_app
