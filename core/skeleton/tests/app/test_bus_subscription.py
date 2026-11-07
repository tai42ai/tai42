"""The app-owned worker-bus subscription seam in ``lifecycle.py``.

The lifecycle joins ONE long-lived worker-bus subscription per process, built once
and never rejoined: the boot read builds the bus (``serve``/``backend`` ``WorkerKind``
or the no-op local variant), and ``app_context`` subscribes with ``_apply_bus_op`` as the
callback and ``_resync_on_ready`` as the on-ready self-resync, and cancels it at shutdown.

* ``_apply_bus_op`` validates a delivered op into its typed model, dispatches it to its
  local admin primitive AND fires the ``on_fleet_op_applied`` handlers with the applied
  op — its declared effects and the seconds left before the apply deadline — the op has
  not fully applied until they finish, so a raising handler fails the op;
* ``_resync_on_ready`` routes the reconnect self-resync through the same apply
  path, so the hook fires for the resync reload too (a celery worker's prefork
  children must not stay stale on the exact path the resync heals);
* the no-op ``WorkerBus.local`` variant is used when no bus is configured, and the
  real process ``app`` proves ``app_context`` spawns the subscription on it and
  cancels it cleanly at shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import fakeredis
import pytest
from pydantic import Field
from pydantic_settings import SettingsConfigDict
from redis.exceptions import ReadOnlyError
from tai42_contract.app import tai42_app
from tai42_contract.backend import FleetOpApplied, FleetOpEffect
from tai42_contract.errors import ClientDisconnectedError
from tai42_contract.storage import ObjectStat, Storage
from tai42_kit.settings import TaiBaseSettings, reset_all_settings

import tai42_skeleton.app.bus as bus_package
from tai42_skeleton.app.bus import (
    FleetResult,
    LocalApplyResult,
    OpOutcome,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerResult,
    WorkerRow,
    WorkerState,
    presence_fresh,
)
from tai42_skeleton.app.bus.ops import (
    CarryRoll,
    ClearTemplateCache,
    EvictTemplate,
    FleetOp,
    ReloadConfig,
    ReprobeFailedMcps,
)
from tai42_skeleton.app.bus_settings import BusRedisSettings, BusSettings
from tai42_skeleton.app.discarded_build import DISCARDED_BUILD_STATUS, mark_discarded_build, state_discarded_build
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.lifecycle import bus_subscription as bus_subscription_module
from tai42_skeleton.app.lifecycle.carried_roll import HeldRoll
from tai42_skeleton.app.recycle import RECYCLED, RecycleError, RecycleReport, RecycleRow, RecycleStop
from tai42_skeleton.config import recycle_roll as recycle_roll_module
from tai42_skeleton.config.recycle_policy import CENSUS_TARGET_KINDS, UNREACHABLE_REPORT
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.template import ResourceManager
from tai42_skeleton.template import resource_manager as rm_mod
from tai42_skeleton.template.settings import TemplateCacheSettings

from .._fakes.bus import FakeBus
from .._fakes.kit_redis import KitFakeRedis
from .._fakes.listing import keys_under

tai42_app.bind(app)


class _Mixin(TaiMCPLifecycleMixin):
    """Concrete-enough mixin for the hook logic: no server, no config manager."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.preset_manager = cast("Any", SimpleNamespace(reconcile_bases=AsyncMock()))

    def _mcp_tools(self, config, tools, preset_names):  # pragma: no cover - unused here
        self._mcp_bound_tools[config.title] = {f"{config.title}_t"}


# -- _build_bus: local when unconfigured, real when configured ----------------


def test_build_bus_is_local_without_a_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    m = _Mixin()
    bus = m._build_bus(WorkerKind.serve)
    assert isinstance(bus, WorkerBus)
    assert bus._local is True
    # The busless variant self-mints its identity at construction.
    assert bus.identity.kind is WorkerKind.serve
    assert bus.identity.name == "serve-1"


def test_build_bus_is_real_with_a_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("TAI_BUS_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    try:
        m = _Mixin()
        bus = m._build_bus(WorkerKind.backend)
        assert bus._local is False
        # A real bus mints name+generation at claim time; its kind is fixed at build.
        assert bus._kind is WorkerKind.backend
    finally:
        reset_all_settings()


# -- _apply_bus_op fires the on_fleet_op_applied hook with the op name and budget -


def _freeze_monotonic(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    monkeypatch.setattr(bus_subscription_module.time, "monotonic", lambda: now)


async def test_apply_bus_op_dispatches_then_fires_hook_with_the_applied_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = _Mixin()
    sentinel = {"status": "ok", "from": "dispatch"}

    async def fake_dispatch(op: FleetOp) -> Any:
        return sentinel

    monkeypatch.setattr(m, "_dispatch_bus_op", fake_dispatch)
    _freeze_monotonic(monkeypatch, 100.0)

    fired: list[FleetOpApplied] = []
    m._on_fleet_op_applied(fired.append)

    result = await m._apply_bus_op({"op": "reload_mcp", "title": "svc"}, deadline=125.0)

    # The dispatch result becomes the op's terminal payload, and the hook fired AFTER
    # the op applied, with the op's declared effects and the seconds left before the deadline.
    assert result is sentinel
    assert fired == [FleetOpApplied(op="reload_mcp", effects=frozenset({FleetOpEffect.registry}), budget=25.0)]


async def test_a_passed_deadline_hands_a_zero_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _Mixin()

    async def fake_dispatch(op: FleetOp) -> Any:
        return None

    monkeypatch.setattr(m, "_dispatch_bus_op", fake_dispatch)
    _freeze_monotonic(monkeypatch, 200.0)
    fired: list[float] = []
    m._on_fleet_op_applied(lambda applied: fired.append(applied.budget))

    await m._apply_bus_op({"op": "reload_config"}, deadline=150.0)

    assert fired == [0.0]


async def test_a_raising_hook_fails_the_op(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _Mixin()

    async def fake_dispatch(op: FleetOp) -> Any:
        return {"status": "ok"}

    monkeypatch.setattr(m, "_dispatch_bus_op", fake_dispatch)

    def boom(_applied: FleetOpApplied) -> None:
        raise RuntimeError("prefork turnover failed")

    m._on_fleet_op_applied(boom)

    # The post-apply hook is part of the op applying, so a raising handler makes
    # the op fail loudly (the subscriber turns it into a terminal ``failed``).
    with pytest.raises(RuntimeError, match="prefork turnover failed"):
        await m._apply_bus_op({"op": "list_failed_mcps"}, deadline=1e9)


async def test_a_two_argument_hook_fails_the_op(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _Mixin()

    async def fake_dispatch(op: FleetOp) -> Any:
        return {"status": "ok"}

    monkeypatch.setattr(m, "_dispatch_bus_op", fake_dispatch)
    m._on_fleet_op_applied(lambda name, budget: None)

    with pytest.raises(TypeError):
        await m._apply_bus_op({"op": "reload_config"}, deadline=1e9)


async def test_self_resync_routes_reload_config_through_the_apply_path(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _Mixin()
    m._bus = cast("Any", FakeBus(origin="serve-1"))
    dispatched: list[str] = []

    async def fake_dispatch(op: FleetOp) -> Any:
        dispatched.append(op.op)
        return {"status": "ok"}

    monkeypatch.setattr(m, "_dispatch_bus_op", fake_dispatch)
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "17")
    reset_all_settings()
    _freeze_monotonic(monkeypatch, 50.0)

    fired: list[tuple[str, float]] = []
    m._on_fleet_op_applied(lambda applied: fired.append((applied.op, applied.budget)))

    try:
        # Boot (first connect): the live config was JUST built by start(), so the resync
        # latches ready WITHOUT a redundant reload_config swap.
        await m._resync_on_ready()
        assert dispatched == []
        assert m._boot_ready.is_set()

        # Reconnect: re-read persisted config through the SAME apply path as a delivered
        # op (a broadcast may have been missed while away), so the hook fires for it too,
        # with the full apply window as its budget.
        await m._resync_on_ready()
    finally:
        reset_all_settings()
    assert dispatched == ["reload_config"]
    assert fired == [("reload_config", 17.0)]


async def test_self_resync_swallows_a_failing_apply_and_stays_live(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    m = _Mixin()
    m._bus = cast("Any", FakeBus(origin="serve-1"))
    # A reconnect (boot-ready already latched), so the resync actually reloads.
    m._boot_ready.set()
    dispatched: list[str] = []

    async def flaky_dispatch(op: FleetOp) -> Any:
        dispatched.append(op.op)
        # Only the resync apply (the first call) blows up; a later delivered op
        # applies cleanly, proving the subscription stayed live.
        if len(dispatched) == 1:
            raise RuntimeError("resync reload blew up")
        return {"status": "ok"}

    monkeypatch.setattr(m, "_dispatch_bus_op", flaky_dispatch)

    # A failing resync must NOT propagate — it runs before the message loop, so a
    # propagating error would kill the subscription with no reconnect. Instead it is
    # ERROR-logged and swallowed.
    with caplog.at_level(logging.ERROR):
        await m._resync_on_ready()

    assert any(r.levelno == logging.ERROR and "self-resync" in r.getMessage() for r in caplog.records)
    # The subscription is still live: a subsequently delivered op still applies.
    result = await m._apply_bus_op({"op": "reload_config"}, deadline=1e9)
    assert result == {"status": "ok"}
    assert dispatched == ["reload_config", "reload_config"]


async def test_self_resync_propagates_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _Mixin()
    m._bus = cast("Any", FakeBus(origin="serve-1"))
    # A reconnect (boot-ready latched), so the resync actually reloads and can cancel.
    m._boot_ready.set()

    async def cancel_dispatch(_op: FleetOp) -> Any:
        raise asyncio.CancelledError

    monkeypatch.setattr(m, "_dispatch_bus_op", cancel_dispatch)

    # Cancellation (shutdown) is NOT the swallowed self-resync failure — it must
    # propagate untouched so the subscription task tears down cleanly.
    with pytest.raises(asyncio.CancelledError):
        await m._resync_on_ready()


async def test_a_successful_self_resync_latches_boot_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _Mixin()
    m._bus = cast("Any", FakeBus(origin="serve-1"))

    async def ok_dispatch(_op: FleetOp) -> Any:
        return {"status": "ok"}

    monkeypatch.setattr(m, "_dispatch_bus_op", ok_dispatch)

    # Not ready until the first self-resync completes: the tool registry is still
    # being (re)built, so a consumer awaiting readiness must stay blocked.
    assert not m._boot_ready.is_set()

    await m._resync_on_ready()

    # The registry is now rebuilt and stable — boot-ready is latched and
    # ``wait_until_ready`` resolves at once.
    assert m._boot_ready.is_set()
    await asyncio.wait_for(m._wait_until_ready(), timeout=1.0)


async def test_a_failing_reconnect_resync_keeps_boot_ready_latched(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    m = _Mixin()
    m._bus = cast("Any", FakeBus(origin="serve-1"))

    # Boot (first connect) skips the reload and latches ready — the registry was
    # already built by start().
    async def ok_dispatch(_op: FleetOp) -> Any:
        return {"status": "ok"}

    monkeypatch.setattr(m, "_dispatch_bus_op", ok_dispatch)
    await m._resync_on_ready()
    assert m._boot_ready.is_set()

    # A subsequent reconnect resync whose reload FAILS is swallowed (the subscription
    # stays live) and never un-latches the one-way boot-ready signal.
    async def boom(_op: FleetOp) -> Any:
        raise RuntimeError("reconnect reload blew up")

    monkeypatch.setattr(m, "_dispatch_bus_op", boom)
    with caplog.at_level(logging.ERROR):
        await m._resync_on_ready()
    assert m._boot_ready.is_set()


def test_on_fleet_op_applied_is_registered_through_the_lifecycle_facet() -> None:
    fired: list[str] = []

    @app.lifecycle.on_fleet_op_applied
    def _handler(applied: FleetOpApplied) -> None:  # pragma: no cover - registration only
        fired.append(applied.op)

    key = f"{_handler.__module__}.{_handler.__qualname__}"
    try:
        assert key in app._fleet_op_applied_handlers
        assert app._fleet_op_applied_handlers[key] is _handler
    finally:
        app._fleet_op_applied_handlers.pop(key, None)


# -- the subscription states a reload refused at its epoch build in its failed reply --


async def _subscribed_failure_payload(m: _Mixin) -> Any:
    """Spawn ``m``'s bus subscription over a bus that records it, and return the failure statement it was given."""
    subscribed: dict[str, Any] = {}
    listening = asyncio.Event()

    async def _subscribe(callback: Any, on_ready: Any = None, *, failure_payload: Any = None) -> None:
        subscribed["failure_payload"] = failure_payload
        listening.set()
        await asyncio.Event().wait()

    m._bus = cast("Any", SimpleNamespace(subscribe=_subscribe))
    m._spawn_bus_subscription()
    await asyncio.wait_for(listening.wait(), timeout=2.0)
    return subscribed["failure_payload"]


async def test_a_delivered_reload_refused_at_its_epoch_build_fails_its_reply_stating_the_discarded_build() -> None:
    m = _Mixin()
    statement = await _subscribed_failure_payload(m)
    try:
        refusal = RuntimeError("plugin migration chain pending")
        mark_discarded_build(refusal)
        state_discarded_build(refusal, ["TAI_X"])

        assert statement({"op": "reload_config"}, refusal) == {
            "status": DISCARDED_BUILD_STATUS,
            "awaiting_respawn": ["TAI_X"],
        }
    finally:
        await m._cancel_bus_subscription()


async def test_a_delivered_reload_failing_any_other_way_fails_its_reply_stating_nothing() -> None:
    m = _Mixin()
    statement = await _subscribed_failure_payload(m)
    try:
        assert statement({"op": "reload_config"}, RuntimeError("reload boom")) is None
    finally:
        await m._cancel_bus_subscription()


async def test_a_join_notice_whose_rebuild_is_refused_at_its_epoch_build_fails_its_reply_stating_nothing() -> None:
    # The notice withdraws applied keys before its rebuild, so the previous epoch keeps serving
    # under an env it was not built under: its state is not the known one a discarded reload leaves.
    m = _Mixin()
    statement = await _subscribed_failure_payload(m)
    try:
        refusal = RuntimeError("plugin migration chain pending")
        mark_discarded_build(refusal)
        state_discarded_build(refusal, [])

        assert statement({"op": "deployment_keys_changed"}, refusal) is None
    finally:
        await m._cancel_bus_subscription()


# -- app_context builds the local bus + spawns/cancels the subscription --------


@pytest.mark.usefixtures("app_context_requiring_the_boot_read")
async def test_app_context_manages_the_subscription_on_the_local_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.settings import reset_all_settings

    # Single-worker, file-mode, no-backend, no-bus: the supported busless shape runs
    # on WorkerBus.local() — the boot read builds it, app_context spawns the
    # subscription on it and cancels it cleanly at shutdown.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    monkeypatch.setattr(app, "_bus", app._build_bus(WorkerKind.serve))

    async with app.app_context(Manifest.model_validate({})):
        assert app.bus._local is True
        task = app._bus_subscription_task
        assert task is not None
        assert not task.done()
        # The local subscribe parks; a foreign op is still applied through the shared
        # callback + hook path.
        await asyncio.sleep(0)

    assert app._bus_subscription_task is None
    assert task.cancelled()


# -- the stop keeps the census row until its last declared wait is over -------


@pytest.fixture
def fake_bus_server(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeServer:
    """Route the worker bus's connections to one in-process Redis server."""
    server = fakeredis.FakeServer()

    @contextlib.asynccontextmanager
    async def fake_ctx(
        client_cls: Any, settings: Any = None, *, fresh: bool = False, **kwargs: Any
    ) -> AsyncIterator[KitFakeRedis]:
        client = KitFakeRedis(server=server, decode_responses=True)
        try:
            yield client
        finally:
            await client.aclose()

    monkeypatch.setattr(bus_package, "client_ctx", fake_ctx)
    return server


@pytest.mark.usefixtures("app_context_requiring_the_boot_read")
@pytest.mark.parametrize("announced_before_the_stop", [True, False], ids=["recycle-op", "plain-stop"])
async def test_the_stop_keeps_the_row_recycling_with_its_drain_through_the_shutdown_handlers(
    monkeypatch: pytest.MonkeyPatch, fake_bus_server: fakeredis.FakeServer, announced_before_the_stop: bool
) -> None:
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    # The runtime phase a serve or backend process declares, stood in for here.
    monkeypatch.setattr(app.exit_drain, "_seconds", lambda: 5.0)
    settings = BusSettings(redis=BusRedisSettings(redis_url="redis://fake"), heartbeat_ttl=5.0)
    bus = WorkerBus(settings, kind=WorkerKind.serve, exit_drain=app.exit_drain.seconds)
    monkeypatch.setattr(app, "_bus", bus)
    probe: Any = KitFakeRedis(server=fake_bus_server, decode_responses=True)
    seen: dict[str, Any] = {}

    async def read_the_row_from_a_shutdown_handler() -> None:
        name = bus.identity.name
        rows = {row.name: row for row in await bus.census()}
        row = rows.get(name)
        seen["subscription_task"] = app._bus_subscription_task
        live = row is not None and row.pttl_ms is not None and row.pttl_ms > 0
        seen["row"] = None if row is None else (row.state, row.drain_seconds, live)
        seen["claim_held"] = await probe.get(settings.slot_key(name)) == bus._claim_token
        seen["declared_drain"] = app.exit_drain.seconds()

    monkeypatch.setitem(app._shutdown_handlers, "test.read_the_row", read_the_row_from_a_shutdown_handler)

    async with app.app_context(Manifest.model_validate({})):
        async with asyncio.timeout(5.0):
            while not any(r.name == bus.identity.name and r.state is WorkerState.ready for r in await bus.census()):
                await asyncio.sleep(0.01)
        name = bus.identity.name
        if announced_before_the_stop:
            await bus.mark_recycling()

    # The subscription ended before the shutdown handlers ran; the row still read
    # ``recycling`` with the drain the process declared, fresh, under the held claim.
    assert seen["subscription_task"] is None
    assert seen["row"] == (WorkerState.recycling, seen["declared_drain"], True)
    assert seen["claim_held"] is True
    # The stop ended the life after its shutdown handlers: the row, its index member and the claim are gone.
    assert await probe.get(settings.presence_key(name)) is None
    assert name not in await probe.smembers(settings.presence_index)
    assert await probe.exists(settings.slot_key(name)) == 0
    await probe.aclose()


@pytest.mark.usefixtures("app_context_requiring_the_boot_read")
async def test_a_failed_leaving_announce_runs_every_stop_step_and_is_raised_once_the_stop_ends(
    monkeypatch: pytest.MonkeyPatch, fake_bus_server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    settings = BusSettings(redis=BusRedisSettings(redis_url="redis://fake"), heartbeat_ttl=5.0)
    bus = WorkerBus(settings, kind=WorkerKind.serve, exit_drain=app.exit_drain.seconds)
    monkeypatch.setattr(app, "_bus", bus)
    probe: Any = KitFakeRedis(server=fake_bus_server, decode_responses=True)
    handled: list[str] = []

    async def an_announce_the_bus_refuses() -> None:
        raise ReadOnlyError("You can't write against a read only replica.")

    async def a_shutdown_handler() -> None:
        handled.append("ran")

    monkeypatch.setattr(bus, "mark_recycling", an_announce_the_bus_refuses)
    monkeypatch.setitem(app._shutdown_handlers, "test.a_shutdown_handler", a_shutdown_handler)

    names: list[str] = []

    async def serve_until_the_row_is_up_then_stop() -> None:
        async with app.app_context(Manifest.model_validate({})):
            async with asyncio.timeout(5.0):
                while not any(r.name == bus.identity.name for r in await bus.census()):
                    await asyncio.sleep(0.01)
            names.append(bus.identity.name)

    with caplog.at_level(logging.ERROR), pytest.raises(ReadOnlyError):
        await serve_until_the_row_is_up_then_stop()
    (name,) = names

    # Every stop step ran: the subscription ended, the shutdown handlers ran, the census life ended.
    assert app._bus_subscription_task is None
    assert handled == ["ran"]
    assert await probe.get(settings.presence_key(name)) is None
    assert await probe.exists(settings.slot_key(name)) == 0
    # The failure was logged at ERROR with its traceback when it happened.
    failed = [
        r
        for r in caplog.records
        if r.levelno == logging.ERROR and isinstance(r.exc_info and r.exc_info[1], ReadOnlyError)
    ]
    assert len(failed) == 1
    await probe.aclose()


@pytest.mark.usefixtures("app_context_requiring_the_boot_read")
async def test_a_failed_census_leave_runs_the_teardown_and_is_raised_once_the_stop_ends(
    monkeypatch: pytest.MonkeyPatch, fake_bus_server: fakeredis.FakeServer
) -> None:
    from tai42_skeleton.app import epoch as epoch_module

    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    settings = BusSettings(redis=BusRedisSettings(redis_url="redis://fake"), heartbeat_ttl=5.0)
    bus = WorkerBus(settings, kind=WorkerKind.serve, exit_drain=app.exit_drain.seconds)
    monkeypatch.setattr(app, "_bus", bus)
    steps: list[str] = []

    async def a_release_the_replica_refuses(*_a: object, **_k: object) -> bool:
        steps.append("leave_census")
        raise ReadOnlyError("You can't write against a read only replica.")

    teardown_resources = app._teardown_resources
    clear_epoch = epoch_module.clear_epoch

    async def recorded_teardown() -> None:
        steps.append("teardown_resources")
        await teardown_resources()

    async def recorded_clear_epoch() -> None:
        steps.append("clear_epoch")
        await clear_epoch()

    monkeypatch.setattr(bus, "_release_claim", a_release_the_replica_refuses)
    monkeypatch.setattr(app, "_teardown_resources", recorded_teardown)
    monkeypatch.setattr(epoch_module, "clear_epoch", recorded_clear_epoch)

    async def serve_until_the_row_is_up_then_stop() -> None:
        async with app.app_context(Manifest.model_validate({})):
            async with asyncio.timeout(5.0):
                while not any(r.name == bus.identity.name for r in await bus.census()):
                    await asyncio.sleep(0.01)

    with pytest.raises(ReadOnlyError):
        await serve_until_the_row_is_up_then_stop()

    # The census leave failed, and the resources and the serving generation were still torn down.
    assert steps == ["leave_census", "teardown_resources", "clear_epoch"]


@pytest.mark.usefixtures("app_context_requiring_the_boot_read")
async def test_a_failed_resource_teardown_still_drops_the_serving_generation_and_is_raised_once_the_stop_ends(
    monkeypatch: pytest.MonkeyPatch, fake_bus_server: fakeredis.FakeServer
) -> None:
    from tai42_skeleton.app import epoch as epoch_module

    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    settings = BusSettings(redis=BusRedisSettings(redis_url="redis://fake"), heartbeat_ttl=5.0)
    bus = WorkerBus(settings, kind=WorkerKind.serve, exit_drain=app.exit_drain.seconds)
    monkeypatch.setattr(app, "_bus", bus)
    steps: list[str] = []
    clear_epoch = epoch_module.clear_epoch
    build_serving_core = app._build_serving_core

    async def a_teardown_whose_pool_close_fails() -> None:
        steps.append("teardown_resources")
        raise ExceptionGroup("shutdown teardown failed", [OSError("pool close failed")])

    async def recorded_clear_epoch() -> None:
        steps.append("clear_epoch")
        await clear_epoch()

    def recorded_build_serving_core() -> Any:
        steps.append("build_serving_core")
        return build_serving_core()

    async def serve_until_the_row_is_up_then_stop() -> None:
        async with app.app_context(Manifest.model_validate({})):
            async with asyncio.timeout(5.0):
                while not any(r.name == bus.identity.name for r in await bus.census()):
                    await asyncio.sleep(0.01)
            monkeypatch.setattr(app, "_teardown_resources", a_teardown_whose_pool_close_fails)
            monkeypatch.setattr(epoch_module, "clear_epoch", recorded_clear_epoch)
            monkeypatch.setattr(app, "_build_serving_core", recorded_build_serving_core)

    with pytest.raises(ExceptionGroup) as raised:
        await serve_until_the_row_is_up_then_stop()

    # The teardown failed, and the serving generation was still dropped and the scaffold rebuilt.
    assert steps == ["teardown_resources", "clear_epoch", "build_serving_core"]
    assert raised.group_contains(OSError, match="pool close failed")


# The bound on the boot ``app_context`` runs before the worker serves. The boot is CPU-bound (it
# imports every plugin, projects the operations and lists the tools) with no wait of its own, so
# coverage tracing triples it and a loaded host stretches it many times over; the bound only
# stops a hung boot from holding the run.
_BOOT_BOUND_S = 60.0


@pytest.mark.usefixtures("app_context_requiring_the_boot_read")
async def test_a_second_stop_signal_landing_while_a_background_task_winds_down_cuts_the_stop_short(
    monkeypatch: pytest.MonkeyPatch, fake_bus_server: fakeredis.FakeServer, caplog: pytest.LogCaptureFixture
) -> None:
    # The backend process answers every stop signal by cancelling its main task (``run_backend``):
    # the first one unwinds into the stop, and a second one cancels the stop itself. Here the
    # second lands while the stop waits for the re-probe task to wind down; the operator reads,
    # at WARNING, the step it cut short and every step that never ran.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    settings = BusSettings(redis=BusRedisSettings(redis_url="redis://fake"), heartbeat_ttl=5.0)
    bus = WorkerBus(settings, kind=WorkerKind.serve, exit_drain=app.exit_drain.seconds)
    monkeypatch.setattr(app, "_bus", bus)
    winding_down = asyncio.Event()
    wound_down = asyncio.Event()
    serving = asyncio.Event()
    steps: list[str] = []

    async def a_reprobe_loop_slow_to_end() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            winding_down.set()
            await wound_down.wait()
            raise

    teardown_resources = app._teardown_resources

    async def recorded_teardown() -> None:
        steps.append("teardown_resources")
        await teardown_resources()

    monkeypatch.setattr(app, "_reprobe_failed_mcps_loop", a_reprobe_loop_slow_to_end)
    monkeypatch.setattr(app, "_teardown_resources", recorded_teardown)

    async def serve_until_stopped() -> None:
        async with app.app_context(Manifest.model_validate({})):
            async with asyncio.timeout(5.0):
                while not any(r.name == bus.identity.name for r in await bus.census()):
                    await asyncio.sleep(0.01)
            serving.set()
            await asyncio.Event().wait()

    main_task = asyncio.create_task(serve_until_stopped())
    serving_waiter = asyncio.create_task(serving.wait())
    try:
        async with asyncio.timeout(_BOOT_BOUND_S):
            await asyncio.wait({serving_waiter, main_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        serving_waiter.cancel()
    if main_task.done():
        # The boot ended before the worker served: raise what ended it.
        await main_task
    main_task.cancel()
    async with asyncio.timeout(5.0):
        await winding_down.wait()
    main_task.cancel()
    wound_down.set()
    with caplog.at_level(logging.INFO, logger="tai42_skeleton.app.lifecycle.lifespan"):
        with pytest.raises(asyncio.CancelledError):
            await main_task
        cut_short = list(steps)
        reported = [(r.levelno, r.getMessage()) for r in caplog.records if r.name.endswith("lifecycle.lifespan")]
        caplog.clear()

        # Finish the stop the second signal cut short, so the process state is clean again.
        await app._stop_serving()
        finished = [(r.levelno, r.getMessage()) for r in caplog.records if r.name.endswith("lifecycle.lifespan")]

    # The stop ended at the second signal: no later step ran, and the stop says so.
    assert cut_short == []
    assert reported == [
        (
            logging.WARNING,
            "shutdown stop cut short by CancelledError during re-probe task cancel, which did not finish;"
            " never ran: MCP starts cancel, interactions reaper cancel, sandbox reaper cancel,"
            " media reaper cancel, monitoring health watch cancel, hook runs stop, credential refreshes close,"
            " shutdown handlers, census leave, resource teardown, serving generation drop, boot scaffold rebuild",
        )
    ]
    # A stop whose every step ran says so.
    assert finished[-1] == (logging.INFO, "shutdown stop finished: every step ran")


@pytest.mark.usefixtures("app_context_requiring_the_boot_read")
async def test_a_roll_handed_to_a_worker_whose_stop_began_with_no_signal_is_refused_as_already_leaving(
    monkeypatch: pytest.MonkeyPatch, fake_bus_server: fakeredis.FakeServer
) -> None:
    # An embedded app's stop: the lifespan shutdown begins with no signal and no recycle decision.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    reset_all_settings()
    settings = BusSettings(redis=BusRedisSettings(redis_url="redis://fake"), heartbeat_ttl=5.0)
    bus = WorkerBus(settings, kind=WorkerKind.serve, exit_drain=app.exit_drain.seconds)
    monkeypatch.setattr(app, "_bus", bus)
    answers: list[dict[str, str]] = []

    async def hand_a_roll_from_a_shutdown_handler() -> None:
        writer = bus.identity
        op = CarryRoll(
            op="carry_roll",
            change="this config change",
            report=FleetResult(op="reload_config", results=[]),
            pinned={},
            writer=writer,
            roll="roll-1",
            answers_within=30.0,
        )
        answers.append(await app.accept_carried_roll(op, answers_within=op.answers_within))

    monkeypatch.setitem(app._shutdown_handlers, "test.hand_a_roll", hand_a_roll_from_a_shutdown_handler)

    async with app.app_context(Manifest.model_validate({})):
        async with asyncio.timeout(5.0):
            while not any(r.name == bus.identity.name and r.state is WorkerState.ready for r in await bus.census()):
                await asyncio.sleep(0.01)

    assert answers == [{"already_leaving": "shutdown"}]
    assert app._carried_roll_tasks == set()


async def test_a_release_roll_op_releases_the_roll_it_names_and_answers_how_it_started() -> None:
    m = _Mixin()
    m._held_rolls["roll-1"] = HeldRoll(change="this config change")
    raw = {"op": "release_roll", "roll": "roll-1", "change": "this config change"}

    assert await m._apply_bus_op(raw, deadline=1e9) == {"started": "this config change"}
    assert m._held_rolls["roll-1"].released.is_set()
    # A second release of the same roll reads that it had started.
    assert await m._apply_bus_op(raw, deadline=1e9) == {"already_started": "this config change"}


async def test_a_release_roll_op_naming_no_held_roll_fails() -> None:
    m = _Mixin()

    with pytest.raises(LookupError, match="holds no roll roll-9 for this config change"):
        await m._apply_bus_op({"op": "release_roll", "roll": "roll-9", "change": "this config change"}, deadline=1e9)


# -- evict_template / clear_template_cache dispatch ---------------------------
#
# A receiving worker's dispatch drops its own compiled-template cache, so a store
# write on another worker never leaves this one rendering the stale body. Driven
# against a REAL ResourceManager over an in-memory provider so the eviction is the
# real cache control, not a stub.


class _StubStorage(Storage):
    """In-memory provider: enough for a compile-once render + cache eviction."""

    def __init__(self, items: dict[str, str]) -> None:
        self.items = dict(items)

    async def load(self, path: str) -> str:
        try:
            return self.items[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    async def list(self, directory: str | None = None) -> list[str]:
        return keys_under(sorted(self.items), directory)

    async def upload(self, path: str, content: str) -> None:
        self.items[path] = content

    async def delete(self, path: str) -> None:
        self.items.pop(path, None)

    async def delete_dir(self, path: str) -> None:
        prefix = path.rstrip("/") + "/"
        for key in [k for k in self.items if k.startswith(prefix)]:
            del self.items[key]

    async def stat(self, path: str) -> ObjectStat:
        content_type, _ = mimetypes.guess_type(path)
        return ObjectStat(content_type=content_type, size=len(await self.load_bytes(path)))

    async def load_range(self, path: str, start: int, length: int) -> bytes:
        return (await self.load_bytes(path))[start : start + length]


def _install_manager(monkeypatch: pytest.MonkeyPatch, items: dict[str, str]) -> ResourceManager:
    monkeypatch.setattr(rm_mod, "template_cache_settings", lambda: TemplateCacheSettings(ttl=300, max_size=8))
    manager = ResourceManager(_StubStorage(items), on_evicted=lambda _eviction: None)
    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(storage=SimpleNamespace(resource_manager=manager)))
    return manager


async def test_dispatch_evict_template_drops_the_worker_compilation(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _install_manager(monkeypatch, {"m.j2": "v1 {{ x }}"})
    assert await manager.render_by_id("m.j2", {"x": 1}) == "v1 1"  # populate this worker's cache
    assert manager.get_cache_info().currsize == 1

    result = await _Mixin()._dispatch_bus_op(EvictTemplate(op="evict_template", path="m.j2"))

    assert result == {"evicted": "m.j2"}
    assert manager.get_cache_info().currsize == 0  # the stale compilation is gone


async def test_dispatch_evict_template_prefix_drops_only_the_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _install_manager(monkeypatch, {"dir/a.j2": "{{ x }}", "other.j2": "{{ y }}"})
    await manager.render_by_id("dir/a.j2", {"x": 1})
    await manager.render_by_id("other.j2", {"y": 2})
    assert manager.get_cache_info().currsize == 2

    result = await _Mixin()._dispatch_bus_op(EvictTemplate(op="evict_template", path="dir", prefix=True))

    assert result == {"evicted": "dir"}
    # Only the compilation under the prefix is dropped; the sibling stays cached.
    assert manager.get_cache_info().currsize == 1
    assert await manager.render_by_id("other.j2", {"y": 9}) == "9"


async def test_an_evict_template_without_a_path_fails_before_anything_applies(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _install_manager(monkeypatch, {"m.j2": "{{ x }}"})
    await manager.render_by_id("m.j2", {"x": 1})
    # A malformed fleet op must fail its confirmation, never apply a partial op.
    with pytest.raises(ValueError, match="path"):
        await _Mixin()._apply_bus_op({"op": "evict_template"}, deadline=1e9)
    assert manager.get_cache_info().currsize == 1


async def test_dispatch_clear_template_cache_drops_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _install_manager(monkeypatch, {"a.j2": "{{ x }}", "b.j2": "{{ y }}"})
    await manager.render_by_id("a.j2", {"x": 1})
    await manager.render_by_id("b.j2", {"y": 2})
    assert manager.get_cache_info().currsize == 2

    result = await _Mixin()._dispatch_bus_op(ClearTemplateCache(op="clear_template_cache"))

    assert result == {"cleared": True}
    assert manager.get_cache_info().currsize == 0


@pytest.mark.parametrize(
    "task_name",
    [
        "tai-worker-bus-subscription",
        "tai-failed-mcp-reprobe",
        "tai-interactions-expiry-reaper",
        "tai-sandbox-reaper",
        "tai-media-retention-reaper",
        "tai-monitoring-health-watch",
    ],
)
async def test_dead_perpetual_task_marks_and_exits_for_every_registration(
    task_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every one of the six perpetual tasks registers the SAME
    ``_on_perpetual_task_done`` seam (the bus subscription, the re-probe loop, all
    three reapers and the monitoring health watch — grep of ``add_done_callback``), so a
    death of any of them, named by its task, marks ``LifecycleState`` and requests this
    process's graceful exit. Parametrized over all six task names so no registration is
    uncovered."""
    m = _Mixin()
    bus = FakeBus()
    m._bus = cast("Any", bus)

    async def boom():
        raise RuntimeError("perpetual task died")

    task = asyncio.create_task(boom(), name=task_name)
    task.add_done_callback(m._on_perpetual_task_done)
    with contextlib.suppress(RuntimeError):
        await task
    await asyncio.sleep(0)  # let the done-callback run

    assert m._dead_perpetual_task == (task_name, "RuntimeError")
    assert bus.exit_requests == [f"perpetual task {task_name} RuntimeError"]


async def test_a_reconnect_resync_hands_its_own_reload_to_the_join(monkeypatch: pytest.MonkeyPatch) -> None:
    # The resyncing worker's own reload is its row in the join's recycle decision: the
    # recycle-class keys it states as moved restart it as they would any worker.
    m = _Mixin()
    m._bus = cast("Any", FakeBus(origin="serve-1"))
    reload = {"status": "ok", "env_keys": 2, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}

    async def fake_dispatch(op: dict[str, Any]) -> Any:
        return reload

    monkeypatch.setattr(m, "_dispatch_bus_op", fake_dispatch)
    joined: list[dict[str, Any]] = []
    cast("Any", m)._spawn_join = joined.append
    m._boot_ready.set()

    await m._resync_on_ready()

    assert joined == [reload]


async def test_a_first_connect_that_withdraws_nothing_joins_as_a_clean_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _Mixin()
    m._bus = cast("Any", FakeBus(origin="serve-1"))
    joined: list[dict[str, Any]] = []
    cast("Any", m)._spawn_join = joined.append

    await m._resync_on_ready()

    assert joined == [{"awaiting_respawn": []}]


async def test_dispatch_reprobe_failed_mcps_reaches_the_admin_with_the_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    async def reprobe(titles: list[str]) -> list[dict[str, Any]]:
        seen.append(list(titles))
        return [{"title": t, "status": "ok"} for t in titles]

    monkeypatch.setattr(tai42_app, "_impl", SimpleNamespace(admin=SimpleNamespace(reprobe_failed_mcps=reprobe)))

    result = await _Mixin()._dispatch_bus_op(ReprobeFailedMcps(op="reprobe_failed_mcps", titles=["a", "b"]))

    # The bare per-title list, the shape the publisher's own apply rides on its self entry.
    assert result == [{"title": "a", "status": "ok"}, {"title": "b", "status": "ok"}]
    assert seen == [["a", "b"]]


# -- the fleet's deployment names: the join withdraw and the join notice ------

_SIBLING_KEY = "BUS_SUBSCRIPTION_TEST_SIBLING_ONLY"


def _sibling_bus(keys: list[str], settings: BusSettings | None = None) -> FakeBus:
    """A bus whose one sibling row publishes ``keys`` as the names its deployment sets."""
    bus = FakeBus(origin="serve-1", remotes=["backend-1"], settings=settings)
    bus.rows[1].deployment_keys = list(keys)
    return bus


def _reload_returns(m: _Mixin, result: dict[str, Any]) -> list[str]:
    """Answer every reload ``m`` dispatches with ``result``; every other op dispatches as built.

    Returns the list the reloads are recorded in.
    """
    dispatch = m._dispatch_bus_op
    reloads: list[str] = []

    async def reload_or_dispatch(op: FleetOp) -> Any:
        if isinstance(op, ReloadConfig):
            reloads.append(op.op)
            return result
        return await dispatch(op)

    cast("Any", m)._dispatch_bus_op = reload_or_dispatch
    return reloads


@pytest.fixture
def applied_sibling_key(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A stored copy of ``_SIBLING_KEY`` applied by this process, as its boot apply left it."""
    from tai42_skeleton.app import applied_env

    loaded_before = set(applied_env._loaded_env_keys)
    monkeypatch.setenv(_SIBLING_KEY, "stored-copy")
    applied_env._loaded_env_keys = {*loaded_before, _SIBLING_KEY}
    yield
    os.environ.pop(_SIBLING_KEY, None)
    applied_env._loaded_env_keys = loaded_before


@pytest.mark.usefixtures("applied_sibling_key")
async def test_the_first_join_withdraws_an_applied_key_the_fleet_owns_and_notifies_the_fleet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tai42_skeleton.app import applied_env

    m = _Mixin()
    bus = _sibling_bus([_SIBLING_KEY])
    m._bus = cast("Any", bus)
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    with caplog.at_level(logging.WARNING):
        await m._resync_on_ready()
        await _join_done(m)

    assert _SIBLING_KEY not in os.environ
    assert _SIBLING_KEY not in applied_env.applied_env_keys()
    lines = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and _SIBLING_KEY in r.getMessage()]
    assert len(lines) == 1
    assert "stored-copy" not in lines[0]
    assert m._boot_ready.is_set()
    # The joiner tells the fleet its names are on the census, so siblings withdraw too.
    assert [call[0] for call in bus.publish_calls] == [{"op": "deployment_keys_changed"}]
    assert bus.publish_calls[0][1] is None
    assert bus.publish_calls[0][2] is not None


async def test_a_reconnect_notifies_the_fleet_again(monkeypatch: pytest.MonkeyPatch) -> None:
    m = _Mixin()
    bus = _sibling_bus([])
    m._bus = cast("Any", bus)

    async def ok_dispatch(_op: FleetOp) -> Any:
        return {"status": "ok", "env_keys": 0, "awaiting_respawn": []}

    monkeypatch.setattr(m, "_dispatch_bus_op", ok_dispatch)
    m._boot_ready.set()
    await m._resync_on_ready()
    await _join_done(m)

    assert [call[0] for call in bus.publish_calls] == [{"op": "deployment_keys_changed"}]


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_join_notice_makes_a_receiver_withdraw_an_applied_key_the_fleet_owns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tai42_skeleton.app import applied_env

    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([_SIBLING_KEY]))
    reloads = _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    with caplog.at_level(logging.WARNING):
        result = await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert result == {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": []}
    assert reloads == ["reload_config"]
    assert _SIBLING_KEY not in os.environ
    assert _SIBLING_KEY not in applied_env.applied_env_keys()
    assert any(r.levelno == logging.WARNING and _SIBLING_KEY in r.getMessage() for r in caplog.records)


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_join_notice_keeps_a_key_no_worker_owns() -> None:
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([]))
    reloads = _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    result = await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    # Nothing withdrawn, nothing rebuilt.
    assert result == {"withdrawn": [], "awaiting_respawn": []}
    assert reloads == []
    assert os.environ[_SIBLING_KEY] == "stored-copy"


class _DownCensusBus(FakeBus):
    """A bus whose census fails the way a severed bus does."""

    async def census(self) -> list[Any]:
        raise ClientDisconnectedError("bus down")


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_first_join_that_cannot_read_the_census_fails_the_resync(caplog: pytest.LogCaptureFixture) -> None:
    m = _Mixin()
    bus = _DownCensusBus(origin="serve-1", remotes=["backend-1"])
    m._bus = cast("Any", bus)

    with caplog.at_level(logging.ERROR):
        await m._resync_on_ready()

    assert any(r.levelno == logging.ERROR and "self-resync failed" in r.getMessage() for r in caplog.records)
    assert not m._boot_ready.is_set()
    assert bus.publish_calls == []
    assert os.environ[_SIBLING_KEY] == "stored-copy"


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_join_notice_that_cannot_read_the_census_fails_the_op() -> None:
    m = _Mixin()
    m._bus = cast("Any", _DownCensusBus(origin="serve-1", remotes=["backend-1"]))

    with pytest.raises(ClientDisconnectedError, match="bus down"):
        await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert os.environ[_SIBLING_KEY] == "stored-copy"


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_reconnect_withdraws_a_key_the_fleet_owns_before_it_reloads(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app import applied_env

    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([_SIBLING_KEY]))
    seen_at_reload: list[bool] = []

    async def ok_dispatch(_op: FleetOp) -> Any:
        seen_at_reload.append(_SIBLING_KEY in os.environ)
        return {"status": "ok", "env_keys": 0, "awaiting_respawn": []}

    monkeypatch.setattr(m, "_dispatch_bus_op", ok_dispatch)
    m._boot_ready.set()
    await m._resync_on_ready()

    assert seen_at_reload == [False]
    assert _SIBLING_KEY not in applied_env.applied_env_keys()


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_first_join_that_withdraws_rebuilds_its_serving_generation_through_the_reload_seam() -> None:
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([_SIBLING_KEY]))
    reloads = _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})
    fired: list[tuple[str, frozenset[FleetOpEffect]]] = []
    m._on_fleet_op_applied(lambda applied: fired.append((applied.op, applied.effects)))

    await m._resync_on_ready()

    assert reloads == ["reload_config"]
    # The rebuild carries the registry effect, so a backend turns its pool over.
    assert fired == [("reload_config", frozenset({FleetOpEffect.registry}))]
    assert m._boot_ready.is_set()


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_first_join_that_withdraws_nothing_rebuilds_nothing() -> None:
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([]))
    reloads = _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    await m._resync_on_ready()

    assert reloads == []
    assert m._boot_ready.is_set()
    assert os.environ[_SIBLING_KEY] == "stored-copy"


_UNRELATED_DRIFT = "SOME_OTHER_RECYCLE_KEY"


@pytest.mark.usefixtures("applied_sibling_key")
@pytest.mark.parametrize("fleet_keys", [[_SIBLING_KEY], []], ids=["withdraws-a-key", "withdraws-nothing"])
async def test_a_join_notice_receiver_reports_every_recycle_class_key_it_still_runs_on(
    monkeypatch: pytest.MonkeyPatch, fleet_keys: list[str]
) -> None:
    # The receiver carries drift from an earlier change the withdraw has no part in.
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus(fleet_keys))
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": [_UNRELATED_DRIFT]})
    monkeypatch.setattr(bus_subscription_module, "awaiting_respawn", lambda: [_UNRELATED_DRIFT])

    result = await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert result == {"withdrawn": fleet_keys, "awaiting_respawn": [_UNRELATED_DRIFT]}


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_sibling_with_unrelated_earlier_drift_is_restarted_by_a_join_as_by_the_next_config_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.config.recycle_policy import recycle_decision

    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([_SIBLING_KEY]))
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": [_UNRELATED_DRIFT]})
    monkeypatch.setattr(bus_subscription_module, "awaiting_respawn", lambda: [_UNRELATED_DRIFT])
    writer = WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=1)
    pinned = {"backend-1": (WorkerKind.backend, 1)}

    def decided(op: str, reply: Any) -> Any:
        report = FleetResult(op=op, results=[WorkerResult(name="backend-1", outcome=OpOutcome.applied, payload=reply)])
        return recycle_decision(report, pinned, writer)

    join = decided("deployment_keys_changed", await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9))
    reload = decided("reload_config", await m._apply_bus_op({"op": "reload_config"}, deadline=1e9))

    assert join.reported["backend-1"].keys == (_UNRELATED_DRIFT,)
    assert join.reported == reload.reported
    assert join.clean == reload.clean == set()


_BUS_URL = "redis://bus.test:6379/0"


class _MemberBus(FakeBus):
    """A bus built under a stored bus URL, whose one sibling's deployment sets that URL; records a recycle mark."""

    def __init__(self) -> None:
        super().__init__(
            origin="serve-1", remotes=["backend-1"], settings=BusSettings(redis=BusRedisSettings(redis_url=_BUS_URL))
        )
        self.rows[1].deployment_keys = ["TAI_BUS_REDIS_URL"]


@pytest.fixture
def applied_stored_bus_url(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A stored copy of the bus URL applied by this process at boot, the bus it is a member of built under it."""
    from tai42_skeleton.app import applied_env

    loaded_before = set(applied_env._loaded_env_keys)
    monkeypatch.setenv("TAI_BUS_REDIS_URL", _BUS_URL)
    applied_env._loaded_env_keys = {*loaded_before, "TAI_BUS_REDIS_URL"}
    reset_all_settings()
    yield
    os.environ.pop("TAI_BUS_REDIS_URL", None)
    applied_env._loaded_env_keys = loaded_before
    reset_all_settings()


def _refusal_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    return [message for message in errors if "TAI_BUS_REDIS_URL" in message]


async def _assert_the_bus_refusal_ended_the_process(m: _Mixin, bus: _MemberBus) -> None:
    assert bus.exit_requests == ["bus settings are no longer deployment-given"]
    # The decision to leave begins the leaving: its census row reads ``recycling``.
    await bus.leaving_announced()
    assert bus.recycling_marks == 1
    assert m.dead_perpetual_task() == (bus_subscription_module.BUS_SUBSCRIPTION_TASK, "BusNotDeploymentGivenError")


@pytest.mark.usefixtures("applied_stored_bus_url")
async def test_a_first_join_whose_withdraw_takes_away_the_bus_url_ends_the_process(
    caplog: pytest.LogCaptureFixture,
) -> None:
    m = _Mixin()
    bus = _MemberBus()
    m._bus = cast("Any", bus)
    reloads = _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    with caplog.at_level(logging.ERROR):
        await m._resync_on_ready()

    errors = _refusal_errors(caplog)
    assert len(errors) == 1
    assert _BUS_URL not in errors[0]
    assert not any("resync retried on the next reconnect" in r.getMessage() for r in caplog.records)
    await _assert_the_bus_refusal_ended_the_process(m, bus)
    assert not m._boot_ready.is_set()
    assert bus.publish_calls == []
    assert reloads == []


@pytest.mark.usefixtures("applied_stored_bus_url")
async def test_a_join_notice_whose_withdraw_takes_away_the_bus_url_fails_the_op_and_ends_the_process(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from tai42_skeleton.app.boot_rules import BusNotDeploymentGivenError

    m = _Mixin()
    bus = _MemberBus()
    m._bus = cast("Any", bus)
    reloads = _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    with caplog.at_level(logging.ERROR), pytest.raises(BusNotDeploymentGivenError, match="TAI_BUS_REDIS_URL"):
        await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert len(_refusal_errors(caplog)) == 1
    await _assert_the_bus_refusal_ended_the_process(m, bus)
    assert reloads == []


@pytest.mark.usefixtures("applied_stored_bus_url")
async def test_a_reconnect_whose_withdraw_takes_away_the_bus_url_ends_the_process_without_reloading(
    caplog: pytest.LogCaptureFixture,
) -> None:
    m = _Mixin()
    bus = _MemberBus()
    m._bus = cast("Any", bus)
    reloads = _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})
    m._boot_ready.set()

    with caplog.at_level(logging.ERROR):
        await m._resync_on_ready()

    assert len(_refusal_errors(caplog)) == 1
    await _assert_the_bus_refusal_ended_the_process(m, bus)
    assert bus.publish_calls == []
    assert reloads == []


_MEMBER_NAMESPACE = "member"


@pytest.fixture
def pending_stored_namespace(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A stored ``TAI_BUS_NAMESPACE`` applied by a reload after this process joined its bus under
    the namespace ``_MEMBER_NAMESPACE``: the process stays a member of that bus until a respawn."""
    from tai42_skeleton.app import applied_env

    loaded_before = set(applied_env._loaded_env_keys)
    monkeypatch.setenv("TAI_BUS_NAMESPACE", "pending")
    applied_env._loaded_env_keys = {*loaded_before, "TAI_BUS_NAMESPACE"}
    reset_all_settings()
    yield
    os.environ.pop("TAI_BUS_NAMESPACE", None)
    applied_env._loaded_env_keys = loaded_before
    reset_all_settings()


@pytest.mark.usefixtures("applied_sibling_key", "pending_stored_namespace")
async def test_a_join_notice_whose_withdraw_leaves_the_bus_where_it_was_withdraws_and_keeps_the_process() -> None:
    m = _Mixin()
    bus = _sibling_bus([_SIBLING_KEY], BusSettings(namespace=_MEMBER_NAMESPACE))
    m._bus = cast("Any", bus)
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    result = await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert result == {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": []}
    assert _SIBLING_KEY not in os.environ
    assert os.environ["TAI_BUS_NAMESPACE"] == "pending"
    assert bus.exit_requests == []
    assert m.dead_perpetual_task() is None


@pytest.mark.usefixtures("pending_stored_namespace")
async def test_a_join_notice_whose_withdraw_returns_the_namespace_to_the_bus_it_runs_on_keeps_the_process() -> None:
    m = _Mixin()
    bus = _sibling_bus(["TAI_BUS_NAMESPACE"], BusSettings(namespace="tai"))
    m._bus = cast("Any", bus)
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    result = await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert result["withdrawn"] == ["TAI_BUS_NAMESPACE"]
    assert "TAI_BUS_NAMESPACE" not in os.environ
    assert bus.exit_requests == []
    assert m.dead_perpetual_task() is None


class _JoinBus(FakeBus):
    """A bus whose siblings answer the join announcement with ``payloads`` and that records a recycle mark.

    ``publish`` applies the publisher's gate to each sibling row: a ready+fresh row answers, a
    fresh ``resyncing`` row answers only an op that declares ``awaited_mid_resync``, and any
    other row lands as its gap outcome, freshness read through the real ``presence_fresh``.
    """

    def __init__(self, payloads: dict[str, dict[str, Any]], *, fleet_keys: list[str], events: list[str]) -> None:
        super().__init__(origin="serve-1", remotes=list(payloads))
        for row in self.rows[1:]:
            row.kind = WorkerKind.backend
        self.rows[1].deployment_keys = list(fleet_keys)
        self._payloads = payloads
        self._events = events

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        await super().publish(op, targets, local, expected_at_start=expected_at_start)
        results = [WorkerResult(name="serve-1", outcome=OpOutcome.applied)]
        for row in self.rows[1:]:
            fresh = presence_fresh(row.pttl_ms, self.ttl)
            if fresh and (
                row.state == WorkerState.ready or (row.state == WorkerState.resyncing and type(op).awaited_mid_resync)
            ):
                results.append(WorkerResult(name=row.name, outcome=OpOutcome.applied, payload=self._payloads[row.name]))
            else:
                outcome = OpOutcome(row.state.value) if fresh else OpOutcome.stale
                results.append(WorkerResult(name=row.name, outcome=outcome, detail=f"state={row.state.value}"))
        return FleetResult(op=op.op, results=results)

    async def mark_recycling(self) -> None:
        self._events.append("mark_recycling")

    def signal_departure(self) -> None:
        self._events.append("exit")
        super().signal_departure()


@pytest.fixture
def recorded_roll(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record the recycle roll the join starts (the join bus records the self-exit); the roll
    recycles every pinned name."""
    seen: dict[str, Any] = {"events": [], "calls": [], "raise": None}

    async def orchestrate(bus: Any, **kwargs: Any) -> RecycleReport:
        seen["events"].append("roll")
        seen["calls"].append(kwargs)
        if seen["raise"] is not None:
            raise seen["raise"]
        return RecycleReport(
            rows=[
                RecycleRow(name=name, kind=kind.value, generation_before=generation, status=RECYCLED)
                for name, (kind, generation) in kwargs["expected"].items()
            ]
        )

    monkeypatch.setattr(bus_subscription_module, "orchestrate_recycle", orchestrate)
    monkeypatch.setattr(recycle_roll_module, "recycle_step_timeout", lambda: 3.0)
    return seen


async def _join_done(m: _Mixin) -> None:
    await asyncio.gather(*m._deployment_keys_join_tasks)


async def test_the_shutdown_leaves_no_join_running_from_a_resync_that_finishes_during_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A reconnect resync that finishes while the shutdown is cancelling the carried rolls would
    # start a join; once the shutdown has cancelled the bus subscription, no join runs on.
    m = _Mixin()
    resync_may_finish = asyncio.Event()

    async def a_join_that_rolls_until_cancelled(_own_reload: dict[str, Any]) -> None:
        await asyncio.Event().wait()

    async def a_subscription_mid_resync() -> None:
        await resync_may_finish.wait()
        m._spawn_join(dict(_CLEAN_JOIN))
        await asyncio.Event().wait()

    async def cancel_carried_rolls_while_the_resync_finishes() -> None:
        resync_may_finish.set()
        for _ in range(3):
            await asyncio.sleep(0)

    monkeypatch.setattr(m, "_join", a_join_that_rolls_until_cancelled)
    monkeypatch.setattr(m, "_cancel_carried_rolls", cancel_carried_rolls_while_the_resync_finishes)
    m._bus_subscription_task = asyncio.create_task(a_subscription_mid_resync())
    await asyncio.sleep(0)

    async with asyncio.timeout(2.0):
        await m._cancel_bus_subscription()

    assert m._deployment_keys_join_tasks == set()
    assert m._bus_subscription_task is None


_CLEAN_JOIN = {"awaiting_respawn": []}


async def test_the_join_restarts_the_workers_that_reported_keys_and_leaves_the_confirmed_clean_running(
    recorded_roll: dict[str, Any],
) -> None:
    m = _Mixin()
    m._bus = cast(
        "Any",
        _JoinBus(
            {
                "backend-1": {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": [_SIBLING_KEY]},
                "backend-2": {"withdrawn": [], "awaiting_respawn": []},
            },
            fleet_keys=[],
            events=recorded_roll["events"],
        ),
    )

    await m._join(dict(_CLEAN_JOIN))

    [call] = recorded_roll["calls"]
    assert call["expected"] == {"backend-1": (WorkerKind.backend, 1), "backend-2": (WorkerKind.backend, 1)}
    decision = call["decision"]
    assert set(decision.reported) == {"backend-1"}
    assert decision.reported["backend-1"].keys == (_SIBLING_KEY,)
    assert decision.clean == {"backend-2"}
    assert not decision.rolls_over("backend-2")
    assert list(call["target_kinds"]) == list(CENSUS_TARGET_KINDS)
    assert decision.writer.name == "serve-1"
    assert call["applier_self_deferred"] is False
    assert recorded_roll["events"] == ["roll"]


async def test_a_join_on_a_worker_already_leaving_announces_nothing_and_logs_it(
    recorded_roll: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    bus = _JoinBus(
        {"backend-1": {"withdrawn": [], "awaiting_respawn": [_SIBLING_KEY]}},
        fleet_keys=[],
        events=recorded_roll["events"],
    )
    m = _Mixin()
    m._bus = cast("Any", bus)
    assert bus.decide_departure("a fleet recycle op") is None

    with caplog.at_level(logging.INFO):
        m._spawn_join(dict(_CLEAN_JOIN))
        await _join_done(m)

    assert bus.publish_calls == []
    # Only the leaving's own recycling announce ran: no roll.
    assert recorded_roll["events"] == ["mark_recycling"]
    leaving = [(r.levelno, r.getMessage()) for r in caplog.records if "already leaving" in r.getMessage()]
    assert leaving == [
        (
            logging.INFO,
            "worker bus: the join of serve-1 on a worker already leaving (a fleet recycle op) — nothing announced",
        )
    ]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert bus.exit_deferred is None


async def test_the_join_asks_a_worker_that_did_not_confirm_the_announcement_once_ready_before_any_restart(
    recorded_roll: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    bus = _JoinBus(
        {name: {"withdrawn": [], "awaiting_respawn": []} for name in ("backend-1", "backend-2")},
        fleet_keys=[],
        events=recorded_roll["events"],
    )
    bus.rows[2].state = WorkerState.recycling
    m = _Mixin()
    m._bus = cast("Any", bus)

    with caplog.at_level(logging.INFO, logger="tai42_skeleton.config.recycle_roll"):
        await m._join(dict(_CLEAN_JOIN))

    [call] = recorded_roll["calls"]
    decision = call["decision"]
    assert dict(decision.unconfirmed) == {"backend-2": "its reload reported recycling"}
    assert decision.reported == {}
    assert decision.clean == {"backend-1"}
    decisions = [r.getMessage() for r in caplog.records if "recycle decision for" in r.getMessage()]
    assert decisions == [
        "recycle decision for the deployment keys of serve-1: restart none; confirm once ready backend-2 (its "
        "reload reported recycling); confirmed clean, left running: backend-1; left running, awaiting a respawn "
        "the stored config did not build for: none; the writer 'serve-1' is clean"
    ]


class _FailedAnnounceBus(_JoinBus):
    """A :class:`_JoinBus` whose announcement publish raises."""

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        raise ConnectionError("bus publish down")


async def test_a_failed_announcement_asks_every_pinned_worker_and_keeps_the_joiner_s_own_clean_row(
    recorded_roll: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    m = _Mixin()
    m._bus = cast(
        "Any",
        _FailedAnnounceBus(
            {"backend-1": {"withdrawn": [], "awaiting_respawn": []}}, fleet_keys=[], events=recorded_roll["events"]
        ),
    )

    with caplog.at_level(logging.ERROR):
        await m._join(dict(_CLEAN_JOIN))

    [call] = recorded_roll["calls"]
    assert call["expected"] == {"backend-1": (WorkerKind.backend, 1)}
    decision = call["decision"]
    assert dict(decision.unconfirmed) == {"backend-1": UNREACHABLE_REPORT}
    # The joiner's own resync reload is its known statement: clean, so it does not restart itself.
    assert decision.writer_reason is None
    assert call["applier_self_deferred"] is False
    assert recorded_roll["events"] == ["roll"]
    assert any("announcing this worker's deployment keys" in r.getMessage() for r in caplog.records)


async def test_the_join_rolls_a_worker_that_was_resyncing_when_it_announced(recorded_roll: dict[str, Any]) -> None:
    # The sibling's row is mid-resync when the joiner announces: it is subscribed, applies the
    # announcement after its resync and reports the key it withdrew, so the join rolls it.
    bus = _JoinBus(
        {"backend-1": {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": [_SIBLING_KEY]}},
        fleet_keys=[],
        events=recorded_roll["events"],
    )
    bus.rows[1].state = WorkerState.resyncing
    m = _Mixin()
    m._bus = cast("Any", bus)

    await m._resync_on_ready()
    await _join_done(m)

    assert recorded_roll["calls"][0]["expected"] == {"backend-1": (WorkerKind.backend, 1)}
    assert set(recorded_roll["calls"][0]["decision"].reported) == {"backend-1"}
    assert recorded_roll["events"] == ["roll"]


class _LeavingAfterAnswerBus(_JoinBus):
    """A :class:`_JoinBus` whose siblings leave the census once they answered the announcement."""

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        result = await super().publish(op, targets, local, expected_at_start=expected_at_start)
        self._remote_rows.clear()
        return result


async def test_the_join_restarts_a_worker_that_reported_keys_and_left_the_census_right_after(
    recorded_roll: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    # The sibling answers the announcement with the key its life runs on off its value, then its
    # life leaves (another change's roll recycles it) before the join reads the census again. Its
    # statement belongs to the life pinned before the announcement, so the roll restarts that life
    # and the join names no worker as awaiting a respawn.
    m = _Mixin()
    m._bus = cast(
        "Any",
        _LeavingAfterAnswerBus(
            {"backend-1": {"withdrawn": [], "awaiting_respawn": [_SIBLING_KEY]}},
            fleet_keys=[],
            events=recorded_roll["events"],
        ),
    )

    with caplog.at_level(logging.ERROR):
        await m._join(dict(_CLEAN_JOIN))

    assert [r.getMessage() for r in caplog.records if "awaiting a respawn" in r.getMessage()] == []
    [call] = recorded_roll["calls"]
    assert call["expected"] == {"backend-1": (WorkerKind.backend, 1)}
    assert call["decision"].reported["backend-1"].generation == 1


class _MalformedRowAtPinBus(_JoinBus):
    """A :class:`_JoinBus` whose first census read meets a malformed presence row, gone by the announcement."""

    def __init__(self, payloads: dict[str, dict[str, Any]], *, fleet_keys: list[str], events: list[str]) -> None:
        super().__init__(payloads, fleet_keys=fleet_keys, events=events)
        self.census_reads = 0
        self.announced = False

    async def census(self) -> list[WorkerRow]:
        self.census_reads += 1
        if self.census_reads == 1:
            from tai42_skeleton.app.bus.models import _PresenceValue

            _PresenceValue.model_validate_json(b"{not a presence row")
        return await super().census()

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        self.announced = True
        return await super().publish(op, targets, local, expected_at_start=expected_at_start)


async def test_a_census_the_join_cannot_parse_before_its_announcement_still_lets_the_join_announce_and_roll(
    recorded_roll: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    # The pin is the join's op-start census: a read that fails pins nothing (WARNING, with its
    # traceback) and the join goes on, so the siblings still withdraw the keys its names own and
    # the one that reported a key is asked once ready, then restarted.
    m = _Mixin()
    bus = _MalformedRowAtPinBus(
        {"backend-1": {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": [_SIBLING_KEY]}},
        fleet_keys=[],
        events=recorded_roll["events"],
    )
    m._bus = cast("Any", bus)

    with caplog.at_level(logging.WARNING):
        await m._join(dict(_CLEAN_JOIN))

    assert bus.announced
    [call] = recorded_roll["calls"]
    assert call["expected"] == {}
    assert set(call["decision"].unconfirmed) == {"backend-1"}
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "op-start census for 'deployment_keys_changed' failed" in warning.getMessage()
    assert warning.exc_info is not None


class _UnparsableCensusBus(_JoinBus):
    """A :class:`_JoinBus` whose every census read meets a malformed presence row, its announcement's included."""

    async def census(self) -> list[WorkerRow]:
        from tai42_skeleton.app.bus.models import _PresenceValue

        _PresenceValue.model_validate_json(b"{not a presence row")
        raise AssertionError("the malformed row always fails to parse")

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        await self.census()
        raise AssertionError("the announcement's census always fails")


async def test_a_join_that_reads_neither_its_census_nor_its_announcement_leaves_whom_to_ask_to_the_roll(
    recorded_roll: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    # No sibling is pinned and none stated its env, so the join knows of no worker: the roll
    # runs and its own census read names whom to ask, rather than skipping as if every worker
    # were clean.
    m = _Mixin()
    bus = _UnparsableCensusBus(
        {"backend-1": {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": [_SIBLING_KEY]}},
        fleet_keys=[],
        events=recorded_roll["events"],
    )
    m._bus = cast("Any", bus)

    with caplog.at_level(logging.INFO):
        await m._join(dict(_CLEAN_JOIN))

    [call] = recorded_roll["calls"]
    assert call["expected"] == {}
    decision = call["decision"]
    assert decision.rolls_over("backend-1")
    assert decision.why_unconfirmed("backend-1") == UNREACHABLE_REPORT
    messages = [r.getMessage() for r in caplog.records]
    assert not [message for message in messages if "is skipped" in message]
    [decided] = [message for message in messages if message.startswith("recycle decision for")]
    assert f"confirm once ready every other worker the roll's census shows ({UNREACHABLE_REPORT})" in decided
    [announce_error] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "the join's roll asks each worker its census shows once it is ready" in announce_error.getMessage()


class _HeldJoinBus(_JoinBus):
    """A :class:`_JoinBus` whose ``publish`` answers only once ``release`` is set."""

    def __init__(self, payloads: dict[str, dict[str, Any]], *, fleet_keys: list[str], events: list[str]) -> None:
        super().__init__(payloads, fleet_keys=fleet_keys, events=events)
        self.release = asyncio.Event()

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        await self.release.wait()
        return await super().publish(op, targets, local, expected_at_start=expected_at_start)


async def test_the_join_announcement_does_not_hold_the_resync(recorded_roll: dict[str, Any]) -> None:
    # The resync returns, boot-ready, while its announcement still collects: the message loop
    # that follows it is free to answer another joiner's announcement meanwhile.
    bus = _HeldJoinBus(
        {"backend-1": {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": [_SIBLING_KEY]}},
        fleet_keys=[],
        events=recorded_roll["events"],
    )
    m = _Mixin()
    m._bus = cast("Any", bus)

    await asyncio.wait_for(m._resync_on_ready(), 1.0)

    assert m._boot_ready.is_set()
    assert recorded_roll["events"] == []
    bus.release.set()
    await _join_done(m)
    assert recorded_roll["events"] == ["roll"]


async def test_a_join_every_worker_confirms_clean_rolls_nothing(recorded_roll: dict[str, Any]) -> None:
    m = _Mixin()
    m._bus = cast(
        "Any",
        _JoinBus(
            {"backend-1": {"withdrawn": [], "awaiting_respawn": []}}, fleet_keys=[], events=recorded_roll["events"]
        ),
    )

    await m._join(dict(_CLEAN_JOIN))

    assert recorded_roll["events"] == []


async def test_a_stopped_join_roll_is_logged_and_does_not_raise(
    recorded_roll: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    report = RecycleReport(
        rows=[RecycleRow(name="backend-1", kind="backend", generation_before=1, status="failed", detail="boom")],
        stopped=RecycleStop(kind="backend", name="backend-1", detail="boom"),
    )
    recorded_roll["raise"] = RecycleError("recycle: boom", report)
    m = _Mixin()
    m._bus = cast(
        "Any",
        _JoinBus(
            {"backend-1": {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": [_SIBLING_KEY]}},
            fleet_keys=[],
            events=recorded_roll["events"],
        ),
    )

    with caplog.at_level(logging.ERROR):
        await m._join({"awaiting_respawn": [_SIBLING_KEY]})

    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("deployment keys of serve-1" in line and "did not converge" in line for line in errors)
    # A stopped roll does not recycle the joiner itself.
    assert recorded_roll["events"] == ["roll"]


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_joiner_whose_own_withdraw_left_recycle_class_keys_recycles_itself_after_the_roll(
    recorded_roll: dict[str, Any],
) -> None:
    m = _Mixin()
    m._bus = cast(
        "Any",
        _JoinBus(
            {"backend-1": {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": [_SIBLING_KEY]}},
            fleet_keys=[_SIBLING_KEY],
            events=recorded_roll["events"],
        ),
    )
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": [_SIBLING_KEY]})

    await m._resync_on_ready()
    await _join_done(m)

    assert recorded_roll["calls"][0]["applier_self_deferred"] is True
    assert recorded_roll["events"] == ["roll", "mark_recycling", "exit"]


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_joiner_whose_withdrawn_keys_are_hot_does_not_recycle_itself(recorded_roll: dict[str, Any]) -> None:
    m = _Mixin()
    m._bus = cast(
        "Any",
        _JoinBus(
            {"backend-1": {"withdrawn": [], "awaiting_respawn": []}},
            fleet_keys=[_SIBLING_KEY],
            events=recorded_roll["events"],
        ),
    )
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    await m._resync_on_ready()
    await _join_done(m)

    assert _SIBLING_KEY not in os.environ
    assert recorded_roll["events"] == []


class _RecycleKeyGroup(TaiBaseSettings):
    """Registered at import: one recycle-class field, so its env name is a recycle-class key."""

    model_config = SettingsConfigDict(env_prefix="BUS_SUBSCRIPTION_TEST_")

    pool: str = Field(default="default-pool", json_schema_extra={"reload": "recycle"})


_RECYCLE_KEY = "BUS_SUBSCRIPTION_TEST_POOL"


@pytest.fixture
def applied_lower_case_recycle_key(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A stored copy of the recycle-class key, applied under a lower-case spelling of its name."""
    from tai42_skeleton.app import applied_env

    spelling = _RECYCLE_KEY.lower()
    loaded_before = set(applied_env._loaded_env_keys)
    monkeypatch.setenv(spelling, "stored-copy")
    applied_env._loaded_env_keys = {*loaded_before, spelling}
    yield spelling
    os.environ.pop(spelling, None)
    applied_env._loaded_env_keys = loaded_before


async def test_a_join_notice_receiver_reports_a_recycle_key_withdrawn_under_another_spelling(
    applied_lower_case_recycle_key: str,
) -> None:
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([_RECYCLE_KEY]))
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": [_RECYCLE_KEY]})

    result = await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert result == {"withdrawn": [applied_lower_case_recycle_key], "awaiting_respawn": [_RECYCLE_KEY]}


async def test_a_first_join_hands_its_reload_naming_a_recycle_key_withdrawn_under_another_spelling_to_the_join(
    applied_lower_case_recycle_key: str,
) -> None:
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([_RECYCLE_KEY]))
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": [_RECYCLE_KEY]})
    joined: list[dict[str, Any]] = []
    cast("Any", m)._spawn_join = joined.append

    await m._resync_on_ready()

    assert joined == [{"status": "ok", "env_keys": 0, "awaiting_respawn": [_RECYCLE_KEY]}]
