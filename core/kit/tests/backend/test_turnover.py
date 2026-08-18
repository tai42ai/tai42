"""Pool turnover after a pool-turnover fleet op.

Only a runtime whose worker processes PERSIST ACROSS JOBS holds a snapshot of
this process's tool registry (or its own compiled-template cache), so only that
runtime gets wired. When it is, the turnover runs inside the op's apply and before
its terminal reply: the manifest env the replacement workers inherit is refreshed
first, the budget is derived from the bus apply window so a stall reports a truthful
``failed``, and a raise propagates rather than reporting a false ``applied``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from typing import ClassVar

import pytest
from tai42_contract.backend.runtime import (
    BUS_APPLY_TIMEOUT_ENV,
    POOL_TURNOVER_FLEET_OPS,
    BackendRuntime,
)

from tai42_kit.backend import ManagedBackend
from tai42_kit.backend.base import _turnover_budget
from tests.backend.fakes import (
    FakeApp,
    FakeForkingBackend,
    FakeOnLoopBackend,
    FakeOnLoopWorker,
    FakeThreadWorker,
    running_runtime,
)

_MANIFEST_ENV = "FAKEBACKEND_TEST_MANIFEST"


# A launch-subcommand map, annotated so a test class declares it as the ClassVar
# the base already types it as.
_Runtimes = Mapping[str, type[BackendRuntime]]


@pytest.fixture(autouse=True)
def _no_ambient_apply_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every budget assertion here is written against the CODED default window.

    A developer (or a CI leg) exporting ``TAI_BUS_APPLY_TIMEOUT`` would otherwise
    move the expected budget out from under six tests, which is exactly the kind
    of host-dependent green the kit conftest exists to prevent.
    """
    monkeypatch.delenv(BUS_APPLY_TIMEOUT_ENV, raising=False)


@pytest.fixture
async def wired(app: FakeApp, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeThreadWorker]:
    """A forking backend mid-launch, with its turnover hook registered."""
    monkeypatch.setenv("FAKEBACKEND_MANIFEST_KEY", _MANIFEST_ENV)
    monkeypatch.setenv(_MANIFEST_ENV, "")
    backend = FakeForkingBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(FakeThreadWorker)
    assert isinstance(runtime, FakeThreadWorker)
    try:
        yield runtime
    finally:
        runtime.release()
        await asyncio.wait_for(task, timeout=5)


def _handler(app: FakeApp):
    assert len(app.lifecycle.fleet_handlers) == 1
    return app.lifecycle.fleet_handlers[0]


async def test_a_runtime_holding_no_snapshot_is_never_wired(app: FakeApp) -> None:
    # An engine that runs work in-process sees a registry mutation directly, so
    # there is nothing to turn over and no hook to register.
    backend = FakeOnLoopBackend()
    task = asyncio.create_task(backend.launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(FakeOnLoopWorker)

    assert app.lifecycle.fleet_handlers == []

    runtime.release()
    await asyncio.wait_for(task, timeout=5)


async def test_the_hook_is_registered_before_the_build(app: FakeApp, monkeypatch: pytest.MonkeyPatch) -> None:
    # A mutating op landing during a long boot must not be missed, so the hook
    # goes in before the engine objects are constructed.
    monkeypatch.setenv("FAKEBACKEND_MANIFEST_KEY", _MANIFEST_ENV)
    handlers_at_build: list[int] = []

    class _Worker(FakeThreadWorker):
        async def build(self) -> None:
            handlers_at_build.append(len(app.lifecycle.fleet_handlers))
            await super().build()

    class _Backend(FakeForkingBackend):
        runtimes: ClassVar[_Runtimes] = {"worker": _Worker}

    task = asyncio.create_task(_Backend().launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(_Worker)

    assert handlers_at_build == [1]

    runtime.release()
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.parametrize("op_name", sorted(POOL_TURNOVER_FLEET_OPS))
async def test_a_pool_turnover_op_turns_the_pool_over(wired: FakeThreadWorker, app: FakeApp, op_name: str) -> None:
    # Every pool-turnover op recycles a persisting pool — a registry mutation the
    # workers snapshot, AND a template eviction their compiled cache would miss (a
    # forking backend's children are bus non-members reached only by turnover).
    await _handler(app)(op_name)

    assert wired.turnovers == [(op_name, 27.0)]


@pytest.mark.parametrize("op_name", ["evict_template", "clear_template_cache"])
async def test_a_template_eviction_op_turns_the_pool_over(wired: FakeThreadWorker, app: FakeApp, op_name: str) -> None:
    # Called out explicitly: a template edit reaches only bus members, so a forking
    # backend's prefork children keep rendering the stale compilation until the pool
    # turns over — the same canonical mechanism a reload rides.
    await _handler(app)(op_name)

    assert wired.turnovers == [(op_name, 27.0)]


@pytest.mark.parametrize("op_name", ["list_failed_mcps", "recycle"])
async def test_a_non_turnover_op_turns_nothing_over(wired: FakeThreadWorker, app: FakeApp, op_name: str) -> None:
    # A query mutates no registry and a recycle ends the process, so replacing
    # workers for either would be pure downtime.
    await _handler(app)(op_name)

    assert wired.turnovers == []
    assert os.environ[_MANIFEST_ENV] == ""


async def test_the_manifest_env_is_refreshed_before_the_pool_turns_over(wired: FakeThreadWorker, app: FakeApp) -> None:
    app.admin.live_manifest = {"tools": ["reloaded"]}

    await _handler(app)("reload_config")

    # Captured INSIDE turn_over_pool: the replacement workers inherit the env, so
    # a refresh landing after the call would hand them the pre-mutation registry.
    assert wired.env_at_turnover[_MANIFEST_ENV] == '{"tools":["reloaded"]}'


async def test_a_failing_turnover_fails_the_op(wired: FakeThreadWorker, app: FakeApp) -> None:
    # The caller is inside the op's apply, so a raise is what makes the terminal
    # reply a truthful ``failed`` instead of a false ``applied``.
    wired.turnover_error = RuntimeError("pool did not come back")

    with pytest.raises(RuntimeError, match="pool did not come back"):
        await _handler(app)("reload_tool")


async def test_a_backend_with_no_dispatch_settings_is_refused_at_turnover(app: FakeApp) -> None:
    class _NoSettingsBackend(ManagedBackend):
        label = "no-settings"
        runtimes: ClassVar[_Runtimes] = {"worker": FakeThreadWorker}

    task = asyncio.create_task(_NoSettingsBackend().launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(FakeThreadWorker)

    with pytest.raises(NotImplementedError, match="_NoSettingsBackend declares a runtime requiring pool turnover"):
        await _handler(app)("reload_mcp")

    runtime.release()
    await asyncio.wait_for(task, timeout=5)


# -- budget -------------------------------------------------------------------


def test_the_budget_sits_a_margin_under_the_bus_apply_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(BUS_APPLY_TIMEOUT_ENV, "45")
    assert _turnover_budget() == 42.0


def test_the_budget_is_floored(monkeypatch: pytest.MonkeyPatch) -> None:
    # A tiny apply window must not collapse the budget to nothing: a confirm that
    # cannot even be attempted reports a failure that is not the pool's.
    monkeypatch.setenv(BUS_APPLY_TIMEOUT_ENV, "6")
    assert _turnover_budget() == 5.0


def test_the_budget_falls_back_to_the_default_window(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BUS_APPLY_TIMEOUT_ENV, raising=False)
    assert _turnover_budget() == 27.0


def test_a_malformed_apply_window_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    # A guessed budget would report a guessed outcome.
    monkeypatch.setenv(BUS_APPLY_TIMEOUT_ENV, "soon")
    with pytest.raises(ValueError, match="soon"):
        _turnover_budget()
