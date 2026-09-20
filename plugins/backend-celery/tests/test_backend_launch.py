"""The Celery vendor bindings: what each runtime hands the Celery CLI, how the
worker is drained and terminated, and how its pool turnover is driven.

Readiness gating, signal wiring, drain-on-cancellation and op filtering are the
shared backend base's and are covered by the kit suite; what is asserted here is
only the half that is Celery's.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from tai42_contract.backend import Backend
from tai42_contract.backend.runtime import ExecutionMode

import tai42_backend_celery.core.backend as backend_module
from tai42_backend_celery.core.backend import (
    CeleryBackend,
    CeleryBeatRuntime,
    CeleryFlowerRuntime,
    CeleryWorkerRuntime,
)
from tai42_backend_celery.core.settings import celery_settings


@pytest.fixture
def restore_argv() -> Iterator[None]:
    old = sys.argv
    yield
    sys.argv = old


@pytest.fixture
def celery_stop_flags() -> Iterator[Any]:
    """The process-wide ``celery.worker.state`` stop flags, blank for the test and
    restored after: the drain binding drives them directly."""
    from celery.worker import state

    old = (state.should_stop, state.should_terminate)
    state.should_stop = None
    state.should_terminate = None
    yield state
    state.should_stop, state.should_terminate = old


@pytest.fixture
def quiet_prefork(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the worker's ``prefork.register()`` without connecting Celery's
    process-wide signals for the rest of the suite."""
    calls: list[str] = []
    monkeypatch.setattr(backend_module.prefork, "register", lambda: calls.append("register"))
    return calls


# -- registration ---------------------------------------------------------------


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


def test_the_three_launch_subcommands_are_declared() -> None:
    assert dict(CeleryBackend.runtimes) == {
        "worker": CeleryWorkerRuntime,
        "beat": CeleryBeatRuntime,
        "flower": CeleryFlowerRuntime,
    }
    # Only the worker consumes queued work; beat and flower own their process.
    assert CeleryWorkerRuntime.mode is ExecutionMode.worker_thread
    assert CeleryWorkerRuntime.consumes_work
    assert CeleryWorkerRuntime.pool_turnover_required
    for scheduler_cls in (CeleryBeatRuntime, CeleryFlowerRuntime):
        assert scheduler_cls.mode is ExecutionMode.inline
        assert not scheduler_cls.consumes_work


def test_dispatch_settings_is_the_celery_env_group() -> None:
    # The base refreshes the manifest env under THIS group's key before a turnover,
    # so the re-forked children read the post-mutation registry.
    assert CeleryBackend().dispatch_settings.manifest_key == celery_settings().manifest_key


# -- what each runtime hands the CLI --------------------------------------------


async def test_launch_requires_a_subcommand() -> None:
    with pytest.raises(ValueError, match="requires a subcommand"):
        await CeleryBackend().launch([])


async def test_launch_rejects_unknown_subcommand() -> None:
    with pytest.raises(ValueError, match="Unknown celery launch subcommand 'serve'"):
        await CeleryBackend().launch(["serve"])


@pytest.mark.parametrize("subcmd", ["beat", "flower"])
@pytest.mark.usefixtures("restore_argv")
async def test_launch_beat_and_flower_run_inline(subcmd: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing else runs on this loop, so they block it and keep Celery's own
    # signal handling — which means the CLI runs on the loop's own thread.
    seen: list[tuple[list[str], threading.Thread]] = []
    monkeypatch.setattr(
        backend_module, "_celery_cli_main", lambda: seen.append((list(sys.argv), threading.current_thread()))
    )

    await CeleryBackend().launch([subcmd, "--extra"])

    argv, thread = seen[0]
    assert argv == ["celery", "-A", "tai42_backend_celery.core.app", subcmd, "--extra"]
    assert thread is threading.main_thread()


@pytest.mark.usefixtures("restore_argv")
async def test_launch_worker_builds_its_argv_then_runs_the_cli_off_loop(
    monkeypatch: pytest.MonkeyPatch, quiet_prefork: list[str]
) -> None:
    """The worker blocks, so the host runs it off the loop; while it runs, this
    process's loop (and its worker-bus subscription) stays responsive."""
    release = threading.Event()
    body_thread: list[threading.Thread] = []

    def _cli() -> None:
        body_thread.append(threading.current_thread())
        release.wait()

    monkeypatch.setattr(backend_module, "_celery_cli_main", _cli)
    task = asyncio.ensure_future(CeleryBackend().launch(["worker", "--concurrency=1"]))
    try:
        for _ in range(3):
            await asyncio.sleep(0.01)
            assert not task.done()
        assert sys.argv == ["celery", "-A", "tai42_backend_celery.core.app", "worker", "--concurrency=1"]
        # The pool turnover is prepared before the worker boots past Celery's
        # setup/ready signals.
        assert quiet_prefork == ["register"]
    finally:
        release.set()
    await asyncio.wait_for(task, timeout=5)
    assert body_thread[0] is not threading.main_thread()


# -- stopping --------------------------------------------------------------------


def test_request_drain_is_idempotent(celery_stop_flags: Any) -> None:
    """The host calls the drain on signal AND again on the cancellation path, so a
    repeat must be the same request — escalation belongs to the cold stop."""
    runtime = CeleryWorkerRuntime.from_args([])

    runtime.request_drain()
    assert celery_stop_flags.should_stop == 0
    assert celery_stop_flags.should_terminate is None

    celery_stop_flags.should_stop = 7  # an exit code the worker already chose
    runtime.request_drain()
    assert celery_stop_flags.should_stop == 7  # untouched: a second drain escalates nothing
    assert celery_stop_flags.should_terminate is None


def test_request_terminate_is_the_cold_stop(celery_stop_flags: Any) -> None:
    CeleryWorkerRuntime.from_args([]).request_terminate()
    assert celery_stop_flags.should_terminate == 0


@pytest.mark.usefixtures("restore_argv")
async def test_cancellation_drains_the_worker_and_waits_for_its_exit(
    monkeypatch: pytest.MonkeyPatch, celery_stop_flags: Any, quiet_prefork: list[str]
) -> None:
    """The recycle path end to end: the launch is cancelled, the binding's drain
    runs, and the run body is awaited to completion before the cancellation is
    re-raised — no live worker thread is left behind."""
    release = threading.Event()
    running = threading.Event()

    def _run() -> None:
        running.set()
        release.wait()

    monkeypatch.setattr(backend_module, "_celery_cli_main", _run)
    drained: list[str] = []
    original = CeleryWorkerRuntime.request_drain

    def _drain(self: CeleryWorkerRuntime) -> None:
        original(self)
        drained.append("warm")
        release.set()  # a real worker returns from the CLI once drained

    monkeypatch.setattr(CeleryWorkerRuntime, "request_drain", _drain)

    task = asyncio.ensure_future(CeleryBackend().launch(["worker"]))
    # Cancelling before the run body is inside would test the wrong thing, so wait
    # for the precondition itself rather than for a duration a slow machine breaks.
    assert await asyncio.to_thread(running.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert drained == ["warm"]
    assert celery_stop_flags.should_stop == 0


# -- pool turnover ----------------------------------------------------------------


async def test_turn_over_pool_runs_the_prefork_turnover_off_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The turnover's control I/O blocks, so it must not run on the serving loop —
    and it spends the budget the caller sized, not one of its own."""
    seen: dict[str, Any] = {}

    def _turnover(reason: str, budget: float) -> None:
        seen["call"] = (reason, budget)
        seen["thread"] = threading.current_thread()

    monkeypatch.setattr(backend_module.prefork, "turnover_local_pool", _turnover)

    await CeleryWorkerRuntime.from_args([]).turn_over_pool(reason="reload_config", budget=27.0)

    assert seen["call"] == ("reload_config", 27.0)
    assert seen["thread"] is not threading.main_thread()


async def test_a_failing_turnover_propagates_to_the_caller(monkeypatch: pytest.MonkeyPatch) -> None:
    # The caller is inside the op's apply: a raise is what reports ``failed``
    # instead of a false ``applied``.
    def _boom(reason: str, budget: float) -> None:
        raise RuntimeError("prefork pool did not fully recycle")

    monkeypatch.setattr(backend_module.prefork, "turnover_local_pool", _boom)

    with pytest.raises(RuntimeError, match="did not fully recycle"):
        await CeleryWorkerRuntime.from_args([]).turn_over_pool(reason="reload_tool", budget=5.0)


def test_the_drain_budget_tracks_the_live_task_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """A warm drain waits for the running task, so the budget has to outlast the
    task budget — read LIVE, so a settings epoch that widens
    ``CELERY_TASK_TIMEOUT`` widens the drain with it."""
    from tai42_kit.settings import reset_all_settings

    instance = CeleryBackend()
    assert instance.drain_timeout == float(celery_settings().task_timeout) + 30.0

    monkeypatch.setenv("CELERY_TASK_TIMEOUT", "1200")
    reset_all_settings()
    try:
        assert instance.drain_timeout == 1230.0
    finally:
        monkeypatch.delenv("CELERY_TASK_TIMEOUT", raising=False)
        reset_all_settings()
