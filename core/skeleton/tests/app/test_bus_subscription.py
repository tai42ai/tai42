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
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_contract.app import tai42_app
from tai42_contract.backend import FleetOpApplied, FleetOpEffect
from tai42_contract.errors import ClientDisconnectedError
from tai42_contract.storage import ObjectStat, Storage
from tai42_kit.settings import TaiBaseSettings, reset_all_settings

from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome, WorkerBus, WorkerKind, WorkerResult
from tai42_skeleton.app.bus.ops import ClearTemplateCache, EvictTemplate, FleetOp, ReloadConfig, ReprobeFailedMcps
from tai42_skeleton.app.bus_settings import BusRedisSettings, BusSettings
from tai42_skeleton.app.instance import app
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin, lifespan
from tai42_skeleton.app.lifecycle import bus_subscription as bus_subscription_module
from tai42_skeleton.app.recycle import RECYCLED, RecycleError, RecycleReport, RecycleRow, RecycleStop
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.template import ResourceManager
from tai42_skeleton.template import resource_manager as rm_mod
from tai42_skeleton.template.settings import TemplateCacheSettings

from .._fakes.bus import FakeBus

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

    async def list(self) -> list[str]:
        return sorted(self.items)

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
    exits: list[int] = []
    monkeypatch.setattr(lifespan, "request_graceful_exit", exits.append)

    async def boom():
        raise RuntimeError("perpetual task died")

    task = asyncio.create_task(boom(), name=task_name)
    task.add_done_callback(m._on_perpetual_task_done)
    with contextlib.suppress(RuntimeError):
        await task
    await asyncio.sleep(0)  # let the done-callback run

    assert m._dead_perpetual_task == (task_name, "RuntimeError")
    assert exits == [os.getpid()]


async def test_a_reconnect_resync_names_its_own_keys_awaiting_a_respawn(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The resyncing worker is its own initiator: nothing in a resync respawns it, so the
    # recycle-class keys its reload states as moved are its own loud verdict.
    m = _Mixin()
    m._bus = cast("Any", FakeBus(origin="serve-1"))

    async def fake_dispatch(op: dict[str, Any]) -> Any:
        return {"status": "ok", "env_keys": 2, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}

    monkeypatch.setattr(m, "_dispatch_bus_op", fake_dispatch)
    m._boot_ready.set()
    with caplog.at_level(logging.ERROR):
        await m._resync_on_ready()

    lines = [r.getMessage() for r in caplog.records if "awaiting a respawn" in r.getMessage()]
    assert len(lines) == 1
    assert "serve-1 (BACKEND_MANIFEST_KEY)" in lines[0]


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


@pytest.mark.usefixtures("applied_sibling_key")
async def test_a_join_notice_receiver_reports_the_recycle_class_keys_it_still_runs_on() -> None:
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([_SIBLING_KEY]))
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": [_SIBLING_KEY, "SOME_OTHER_RECYCLE_KEY"]})
    fired: list[tuple[str, frozenset[FleetOpEffect]]] = []
    m._on_fleet_op_applied(lambda applied: fired.append((applied.op, applied.effects)))

    result = await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    # Only the withdrawn names are this change's to report.
    assert result == {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": [_SIBLING_KEY]}
    assert ("reload_config", frozenset({FleetOpEffect.registry})) in fired


_BUS_URL = "redis://bus.test:6379/0"


class _MemberBus(FakeBus):
    """A bus built under a stored bus URL, whose one sibling's deployment sets that URL; records a recycle mark."""

    def __init__(self) -> None:
        super().__init__(
            origin="serve-1", remotes=["backend-1"], settings=BusSettings(redis=BusRedisSettings(redis_url=_BUS_URL))
        )
        self.rows[1].deployment_keys = ["TAI_BUS_REDIS_URL"]
        self.recycling_marks = 0

    async def mark_recycling(self) -> None:
        self.recycling_marks += 1


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


@pytest.fixture
def requested_exits(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record the pids a graceful exit is requested for."""
    exits: list[int] = []
    monkeypatch.setattr(bus_subscription_module, "request_graceful_exit", exits.append)
    return exits


def _refusal_errors(caplog: pytest.LogCaptureFixture) -> list[str]:
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    return [message for message in errors if "TAI_BUS_REDIS_URL" in message]


def _assert_the_bus_refusal_ended_the_process(m: _Mixin, bus: _MemberBus, exits: list[int]) -> None:
    assert exits == [bus.identity.pid]
    assert bus.recycling_marks == 0
    assert m.dead_perpetual_task() == (bus_subscription_module.BUS_SUBSCRIPTION_TASK, "BusNotDeploymentGivenError")


@pytest.mark.usefixtures("applied_stored_bus_url")
async def test_a_first_join_whose_withdraw_takes_away_the_bus_url_ends_the_process(
    requested_exits: list[int], caplog: pytest.LogCaptureFixture
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
    _assert_the_bus_refusal_ended_the_process(m, bus, requested_exits)
    assert not m._boot_ready.is_set()
    assert bus.publish_calls == []
    assert reloads == []


@pytest.mark.usefixtures("applied_stored_bus_url")
async def test_a_join_notice_whose_withdraw_takes_away_the_bus_url_fails_the_op_and_ends_the_process(
    requested_exits: list[int], caplog: pytest.LogCaptureFixture
) -> None:
    from tai42_skeleton.app.boot_rules import BusNotDeploymentGivenError

    m = _Mixin()
    bus = _MemberBus()
    m._bus = cast("Any", bus)
    reloads = _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    with caplog.at_level(logging.ERROR), pytest.raises(BusNotDeploymentGivenError, match="TAI_BUS_REDIS_URL"):
        await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert len(_refusal_errors(caplog)) == 1
    _assert_the_bus_refusal_ended_the_process(m, bus, requested_exits)
    assert reloads == []


@pytest.mark.usefixtures("applied_stored_bus_url")
async def test_a_reconnect_whose_withdraw_takes_away_the_bus_url_ends_the_process_without_reloading(
    requested_exits: list[int], caplog: pytest.LogCaptureFixture
) -> None:
    m = _Mixin()
    bus = _MemberBus()
    m._bus = cast("Any", bus)
    reloads = _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})
    m._boot_ready.set()

    with caplog.at_level(logging.ERROR):
        await m._resync_on_ready()

    assert len(_refusal_errors(caplog)) == 1
    _assert_the_bus_refusal_ended_the_process(m, bus, requested_exits)
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
async def test_a_join_notice_whose_withdraw_leaves_the_bus_where_it_was_withdraws_and_keeps_the_process(
    requested_exits: list[int],
) -> None:
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([_SIBLING_KEY], BusSettings(namespace=_MEMBER_NAMESPACE)))
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    result = await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert result == {"withdrawn": [_SIBLING_KEY], "awaiting_respawn": []}
    assert _SIBLING_KEY not in os.environ
    assert os.environ["TAI_BUS_NAMESPACE"] == "pending"
    assert requested_exits == []
    assert m.dead_perpetual_task() is None


@pytest.mark.usefixtures("pending_stored_namespace")
async def test_a_join_notice_whose_withdraw_returns_the_namespace_to_the_bus_it_runs_on_keeps_the_process(
    requested_exits: list[int],
) -> None:
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus(["TAI_BUS_NAMESPACE"], BusSettings(namespace="tai")))
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": []})

    result = await m._apply_bus_op({"op": "deployment_keys_changed"}, deadline=1e9)

    assert result["withdrawn"] == ["TAI_BUS_NAMESPACE"]
    assert "TAI_BUS_NAMESPACE" not in os.environ
    assert requested_exits == []
    assert m.dead_perpetual_task() is None


class _JoinBus(FakeBus):
    """A bus whose siblings answer the join announcement with ``payloads`` and that records a recycle mark."""

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
        results += [
            WorkerResult(name=name, outcome=OpOutcome.applied, payload=payload)
            for name, payload in self._payloads.items()
        ]
        return FleetResult(op=op.op, results=results)

    async def mark_recycling(self) -> None:
        self._events.append("mark_recycling")


@pytest.fixture
def recorded_roll(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Record the recycle roll the join starts, and the self-exit it requests; the roll recycles every pinned name."""
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
    monkeypatch.setattr(bus_subscription_module, "_recycle_step_timeout", lambda: 3.0)
    monkeypatch.setattr(bus_subscription_module, "request_graceful_exit", lambda pid: seen["events"].append("exit"))
    return seen


async def _roll_done(m: _Mixin) -> None:
    await asyncio.gather(*m._deployment_keys_recycle_tasks)


async def test_the_join_rolls_a_recycle_over_exactly_the_workers_that_reported_keys(
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

    await m._announce_deployment_keys(self_recycle_keys=[])
    await _roll_done(m)

    [call] = recorded_roll["calls"]
    assert call["expected"] == {"backend-1": (WorkerKind.backend, 1)}
    assert call["pinned_only"] is True
    assert list(call["target_kinds"]) == [WorkerKind.backend]
    assert call["excluded_name"] == "serve-1"
    assert call["applier_self_deferred"] is False
    assert recorded_roll["events"] == ["roll"]


async def test_a_join_nobody_reports_keys_for_rolls_nothing(recorded_roll: dict[str, Any]) -> None:
    m = _Mixin()
    m._bus = cast(
        "Any",
        _JoinBus(
            {"backend-1": {"withdrawn": [], "awaiting_respawn": []}}, fleet_keys=[], events=recorded_roll["events"]
        ),
    )

    await m._announce_deployment_keys(self_recycle_keys=[])
    await _roll_done(m)

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
        await m._announce_deployment_keys(self_recycle_keys=[_SIBLING_KEY])
        await _roll_done(m)

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
    await _roll_done(m)

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
    await _roll_done(m)

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


async def test_a_first_join_recycles_itself_for_a_recycle_key_withdrawn_under_another_spelling(
    applied_lower_case_recycle_key: str,
) -> None:
    m = _Mixin()
    m._bus = cast("Any", _sibling_bus([_RECYCLE_KEY]))
    _reload_returns(m, {"status": "ok", "env_keys": 0, "awaiting_respawn": [_RECYCLE_KEY]})
    announced: list[list[str]] = []

    async def record(*, self_recycle_keys: list[str]) -> None:
        announced.append(self_recycle_keys)

    cast("Any", m)._announce_deployment_keys = record

    await m._resync_on_ready()

    assert announced == [[_RECYCLE_KEY]]
