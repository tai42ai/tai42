"""Pool turnover after a fleet op that declares an effect.

Only a runtime whose worker processes PERSIST ACROSS JOBS holds a snapshot of
this process's tool registry (or its own compiled-template cache), so only that
runtime gets wired. When it is, the turnover runs inside the op's apply and before
its terminal reply: the manifest env the replacement workers inherit is refreshed
first, the budget is what the host hands over of the apply window less a margin, so a stall
reports a truthful ``failed``, and a raise propagates rather than reporting a false ``applied``.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Mapping
from typing import ClassVar

import pytest
from tai42_contract.backend.runtime import BackendRuntime, FleetOpApplied, FleetOpEffect

from tai42_kit.backend import ManagedBackend

from .fakes import (
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


def _applied(op: str, *effects: FleetOpEffect, budget: float = 30.0) -> FleetOpApplied:
    return FleetOpApplied(op=op, effects=frozenset(effects), budget=budget)


@pytest.mark.parametrize(
    "effects",
    [(FleetOpEffect.registry,), (FleetOpEffect.templates,), (FleetOpEffect.registry, FleetOpEffect.templates)],
    ids=["registry", "templates", "both"],
)
async def test_an_op_with_an_effect_turns_the_pool_over(
    wired: FakeThreadWorker, app: FakeApp, effects: tuple[FleetOpEffect, ...]
) -> None:
    # A registry change the workers snapshot and a template eviction their compiled cache would
    # miss both recycle a persisting pool (a forking backend's children are bus non-members
    # reached only by turnover); the op's name rides only as the turnover reason.
    await _handler(app)(_applied("probe_op", *effects))

    assert wired.turnovers == [("probe_op", 27.0)]


async def test_an_op_declaring_no_effect_turns_nothing_over(wired: FakeThreadWorker, app: FakeApp) -> None:
    # A query changes nothing and a recycle ends the process, so replacing workers for either
    # would be pure downtime.
    await _handler(app)(_applied("probe_query"))

    assert wired.turnovers == []
    assert os.environ[_MANIFEST_ENV] == ""


async def test_the_manifest_env_is_refreshed_before_the_pool_turns_over(wired: FakeThreadWorker, app: FakeApp) -> None:
    app.admin.live_manifest = {"tools": ["reloaded"]}

    await _handler(app)(_applied("reload_config", FleetOpEffect.registry))

    # Captured INSIDE turn_over_pool: the replacement workers inherit the env, so
    # a refresh landing after the call would hand them the pre-mutation registry.
    assert wired.env_at_turnover[_MANIFEST_ENV] == '{"tools":["reloaded"]}'


async def test_a_failing_turnover_fails_the_op(wired: FakeThreadWorker, app: FakeApp) -> None:
    # The caller is inside the op's apply, so a raise is what makes the terminal
    # reply a truthful ``failed`` instead of a false ``applied``.
    wired.turnover_error = RuntimeError("pool did not come back")

    with pytest.raises(RuntimeError, match="pool did not come back"):
        await _handler(app)(_applied("reload_tool", FleetOpEffect.registry))


async def test_a_backend_with_no_dispatch_settings_is_refused_at_turnover(app: FakeApp) -> None:
    class _NoSettingsBackend(ManagedBackend):
        label = "no-settings"
        runtimes: ClassVar[_Runtimes] = {"worker": FakeThreadWorker}

    task = asyncio.create_task(_NoSettingsBackend().launch(["worker"]))
    app.lifecycle.mark_ready()
    runtime = await running_runtime(FakeThreadWorker)

    with pytest.raises(NotImplementedError, match="_NoSettingsBackend declares a runtime requiring pool turnover"):
        await _handler(app)(_applied("reload_mcp", FleetOpEffect.registry))

    runtime.release()
    await asyncio.wait_for(task, timeout=5)


# -- budget -------------------------------------------------------------------


@pytest.mark.parametrize(("handed", "expected"), [(45.0, 42.0), (30.0, 27.0), (6.0, 5.0), (0.0, 5.0)])
async def test_the_budget_is_the_handed_window_less_a_margin_floored(
    wired: FakeThreadWorker, app: FakeApp, handed: float, expected: float
) -> None:
    # The host hands what is left of the apply window; the turnover keeps a margin under
    # it so a confirm-or-raise lands before the report cut, and a tiny window never
    # collapses the budget to nothing.
    await _handler(app)(_applied("reload_config", FleetOpEffect.registry, budget=handed))

    assert wired.turnovers == [("reload_config", expected)]
