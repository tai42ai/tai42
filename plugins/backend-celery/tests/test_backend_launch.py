"""Backend registration, launch roles, and worker shutdown plumbing."""

from __future__ import annotations

import asyncio
import sys
import threading
from typing import Any

import pytest
from tai42_contract.backend import Backend

import tai42_backend_celery.core.backend as backend_module
from tai42_backend_celery.core.backend import CeleryBackend, _request_worker_shutdown


def test_backend_is_registered_on_import(stub_app) -> None:
    assert stub_app.backends.registered_cls is CeleryBackend
    assert isinstance(stub_app.backends.instance, CeleryBackend)
    assert isinstance(stub_app.backends.instance, Backend)


def test_top_package_import_is_the_whole_discovery_surface(stub_app) -> None:
    """One ``import tai42_backend_celery`` (the manifest ``backend_module``)
    registers the backend, the tool surface, and the three extensions."""
    import tai42_backend_celery

    assert tai42_backend_celery.CeleryBackend is CeleryBackend
    for marker in (
        "backend_list_schedules",
        "backend_delete_schedule",
        "backend_export_schedules",
        "backend_import_schedules",
    ):
        assert marker in stub_app.tools.registered
    for extension_name in ("sync_task", "schedule_task", "async_task"):
        assert extension_name in stub_app.extensions.registered


async def test_launch_requires_a_subcommand() -> None:
    with pytest.raises(ValueError, match="requires a subcommand"):
        await CeleryBackend().launch([])


async def test_launch_rejects_unknown_subcommand() -> None:
    with pytest.raises(ValueError, match="Unknown Celery launch subcommand 'serve'"):
        await CeleryBackend().launch(["serve"])


@pytest.mark.parametrize("subcmd", ["beat", "flower"])
async def test_launch_beat_and_flower_run_inline(subcmd: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(backend_module, "_celery_cli_main", lambda: calls.append(list(sys.argv)))
    old_argv = sys.argv
    try:
        await CeleryBackend().launch([subcmd, "--extra"])
    finally:
        sys.argv = old_argv
    assert calls == [["celery", "-A", "tai42_backend_celery.core.app", subcmd, "--extra"]]


async def test_launch_worker_keeps_the_loop_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker runs off-loop: while it runs, other coroutines still execute."""
    release = threading.Event()
    monkeypatch.setattr(backend_module, "_celery_cli_main", release.wait)
    old_argv = sys.argv
    backend = CeleryBackend()
    task = asyncio.ensure_future(backend.launch(["worker"]))
    try:
        # The loop is responsive while the worker thread runs.
        for _ in range(3):
            await asyncio.sleep(0.01)
            assert not task.done()
        assert sys.argv == ["celery", "-A", "tai42_backend_celery.core.app", "worker"]
    finally:
        sys.argv = old_argv
        release.set()
    await asyncio.wait_for(task, timeout=5)


async def test_launch_worker_waits_for_boot_ready_before_starting(
    stub_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker must not start (nor fork its pool) until the boot-ready latch
    opens: a pool child forked against a half-built tool registry fails every job
    routed to it permanently."""
    started = threading.Event()
    release = threading.Event()

    def _cli() -> None:
        started.set()
        release.wait()

    monkeypatch.setattr(backend_module, "_celery_cli_main", _cli)
    stub_app.lifecycle.ready.clear()
    old_argv = sys.argv
    task = asyncio.ensure_future(CeleryBackend().launch(["worker"]))
    try:
        # While the latch is closed the worker stays unstarted.
        for _ in range(3):
            await asyncio.sleep(0.01)
            assert not started.is_set()
            assert not task.done()
        # Opening the latch releases the worker start.
        stub_app.lifecycle.ready.set()
        await asyncio.wait_for(asyncio.to_thread(started.wait, 5), timeout=5)
        assert started.is_set()
    finally:
        release.set()
        sys.argv = old_argv
    await asyncio.wait_for(task, timeout=5)


async def test_launch_worker_readiness_timeout_raises_without_starting(
    stub_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A boot that never becomes ready fails loudly at the timeout, and the worker
    is never started."""
    started: list[str] = []
    monkeypatch.setattr(backend_module, "_celery_cli_main", lambda: started.append("cli"))
    monkeypatch.setattr(backend_module, "_APP_READY_TIMEOUT_SECONDS", 0.05)
    stub_app.lifecycle.ready.clear()
    old_argv = sys.argv
    try:
        with pytest.raises(RuntimeError, match=r"did not become ready within 0\.05s"):
            await CeleryBackend().launch(["worker"])
    finally:
        sys.argv = old_argv
    assert started == []


async def test_launch_worker_cancellation_requests_shutdown_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    requested: list[str] = []
    monkeypatch.setattr(backend_module, "_celery_cli_main", release.wait)

    def _fake_shutdown_request() -> None:
        requested.append("warm")
        release.set()

    monkeypatch.setattr(backend_module, "_request_worker_shutdown", _fake_shutdown_request)
    old_argv = sys.argv
    backend = CeleryBackend()
    task = asyncio.ensure_future(backend.launch(["worker"]))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=5)
    finally:
        sys.argv = old_argv
    # Cancellation asked the worker to stop and waited for its exit.
    assert requested == ["warm"]


async def test_launch_worker_signal_handler_install_failure_degrades_loudly(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(backend_module, "_celery_cli_main", lambda: None)
    loop = asyncio.get_running_loop()

    def _refuse(sig: Any, cb: Any) -> None:
        raise RuntimeError("signals unsupported here")

    monkeypatch.setattr(loop, "add_signal_handler", _refuse)
    old_argv = sys.argv
    try:
        with caplog.at_level("WARNING"):
            await CeleryBackend().launch(["worker"])
    finally:
        sys.argv = old_argv
    assert sum("cannot install" in r.message for r in caplog.records) == 2


def test_request_worker_shutdown_escalates_warm_then_cold() -> None:
    from celery.worker import state

    old_stop, old_terminate = state.should_stop, state.should_terminate
    state.should_stop = None
    state.should_terminate = None
    try:
        _request_worker_shutdown()
        assert state.should_stop == 0
        assert state.should_terminate is None
        _request_worker_shutdown()
        assert state.should_terminate == 0
    finally:
        state.should_stop = old_stop
        state.should_terminate = old_terminate
