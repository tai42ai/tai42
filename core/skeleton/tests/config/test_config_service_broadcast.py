"""Unit oracles for :class:`~tai42_skeleton.config.service.ConfigService` — the post-persist
reload + broadcast tail and its failure discipline: a local reload that fails after the
persist landed still broadcasts and then re-raises with the fleet report; a raw broadcast
fault becomes a :class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError` carrying
the honest bus-unreachable shape; an unconfirmed origin is a loud ERROR log but a returned
success; expected membership is pinned to op start, not publish time; the
backend-needs-bus and backend-needs-hooks-store invariants reject both directions; and an
env change that leaves the fleet apply window below the retire's drain floor is refused
before it persists.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app import instance
from tai42_skeleton.app.boot_rules import (
    BackendNeedsBusError,
    BackendNeedsHooksStoreError,
    WorkersNeedBusError,
    WorkersNeedHooksStoreError,
    WorkersNeedSubMcpStoreError,
)
from tai42_skeleton.app.bus import OpOutcome
from tai42_skeleton.app.epoch import Epoch
from tai42_skeleton.config.file_manager import FileConfigManager
from tai42_skeleton.operations._broadcast import FleetBroadcastError

from .._fakes.combo_check_app import CHECK_DOCUMENT, booted_check_app, clean_server, factory_calls
from .fake_pipeline import (
    FakeConfigStore,
    FakeReloadAdmin,
    RecordingBus,
    _no_bus,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)

# ---------------------------------------------------------------------------
# Failure discipline
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures(clean_server.__name__)
async def test_a_combo_on_a_base_not_bound_passes_the_gate_and_the_reload_refusal_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    async with booted_check_app():
        store = FakeConfigStore(manifest=dict(CHECK_DOCUMENT))
        admin = FakeReloadAdmin(raise_reload=RuntimeError("Tools 'fresh' not found in server."))
        service, _admin, _bus = _service(store, admin=admin, combo_check=instance.app.extensions.check_combo)
        factory_calls().clear()
        combo = [{"name": "labelled", "config": {"label": "ok"}}]

        def attach(document: dict[str, Any]) -> None:
            document["tools"][0]["extensions"] = {"fresh": [combo]}

        with pytest.raises(FleetBroadcastError, match="'fresh' not found"):
            await service.apply_change(attach)

        assert factory_calls() == []
        assert store.manifest["tools"][0]["extensions"] == {"fresh": [combo]}
        assert admin.calls == 1


async def test_local_reload_failure_after_persist_still_broadcasts_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    admin = FakeReloadAdmin(raise_reload=RuntimeError("reload boom"))
    service, _admin, bus = _service(store, admin=admin)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    # The persist landed; the failed local reload does NOT strand the fleet.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert len(bus.publish_calls) == 1
    _op, _targets, local = bus.publish_calls[0]
    assert local is not None
    assert local.outcome == OpOutcome.failed
    # The fleet report the broadcast produced rides the raised error.
    assert exc.value.report.op == "reload_config"


async def test_broadcast_raise_after_persist_becomes_fleet_broadcast_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    # A non-transport broadcast fault the bus does NOT fold into a returned
    # bus-unreachable report (e.g. a redis ResponseError, or a malformed presence key
    # the census cannot parse) — it raises RAW out of publish AFTER the persist landed.
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, admin, _bus = _service(store, bus=bus)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    # The raw broadcast error was wrapped as FleetBroadcastError, never propagated raw,
    # and rides as the cause.
    assert isinstance(exc.value.__cause__, RuntimeError)
    # The persist DID land — the committed mutation is in the store — and the local
    # reload ran before the broadcast raised.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert admin.calls == 1
    # The error carries the honest bus-unreachable report (no origin list, only error).
    assert exc.value.report.op == "reload_config"
    assert exc.value.report.reachable is False
    assert exc.value.report.results == []
    assert "ResponseError" in (exc.value.report.error or "")


async def test_apply_replace_broadcast_raise_after_persist_becomes_fleet_broadcast_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": [{"title": "old", "config": {"url": "http://old"}}]})
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, admin, _bus = _service(store, bus=bus)

    document = {"mcp": [{"title": "new", "config": {"url": "http://new"}}]}
    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_replace(document)

    # apply_replace honors the same post-persist contract: the replace committed, then
    # the raw broadcast error surfaced as FleetBroadcastError with the unreachable report.
    assert store.manifest == document
    assert admin.calls == 1
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert exc.value.report.reachable is False


# ---------------------------------------------------------------------------
# Expected membership is pinned to op start, not to publish time
#
# publish censuses when it is called — after the local reload. A worker whose presence
# fades across a reimporting reload would drop off that census and the report would read
# converged without it, so the pipeline snapshots membership BEFORE the reload.
# ---------------------------------------------------------------------------


async def test_expected_membership_is_censused_before_the_local_reload(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"])
    # The sibling is live when the op begins and its presence fades DURING the reload.
    admin = FakeReloadAdmin(during_reload=lambda: bus.live.discard("serve-w1"))
    service, _admin, _bus = _service(store, admin=admin, bus=bus)

    await service.apply_replace({"mcp": [{"title": "new", "config": {"url": "http://new"}}]})

    # Read first, so the sibling is still owed a confirmation; a snapshot taken after the
    # reload would be empty and the op would converge without it.
    assert bus.live == set()
    assert bus.expected_at_start_calls == [{"serve-w1": 1}]


async def test_op_start_census_failure_is_loud_but_never_aborts_the_reload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The snapshot rides on top of publish's own census, which degrades a dead bus to an
    # honest unreachable report — so a census blip must not turn a survivable post-persist
    # reload into a raise. Loud, because the op then runs on the membership the snapshot
    # exists to correct.
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"], census_error=ConnectionError("bus census unreachable"))
    service, admin, _bus = _service(store, bus=bus)

    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.operations._broadcast"):
        result = await service.apply_replace({"mcp": [{"title": "new", "config": {"url": "http://new"}}]})

    assert admin.calls == 1
    assert result.fleet.ok
    assert bus.expected_at_start_calls == [None]
    warnings = [
        r for r in caplog.records if r.levelno == logging.WARNING and r.name == "tai42_skeleton.operations._broadcast"
    ]
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert "op-start census" in warnings[0].getMessage()


async def test_broadcast_raise_with_local_reload_failure_is_single_fleet_broadcast_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    admin = FakeReloadAdmin(raise_reload=RuntimeError("reload boom"))
    bus = RecordingBus(publish_error=RuntimeError("ResponseError: WRONGTYPE"))
    service, _admin, _bus = _service(store, admin=admin, bus=bus)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(FleetBroadcastError) as exc:
        await service.apply_change(add_server)

    # Both the local reload AND the broadcast failed after the persist landed — a SINGLE
    # FleetBroadcastError surfaces, carrying the broadcast error as cause and an
    # unreachable report whose error notes the local reload failure too.
    assert store.persisted == [{"mcp": [{"title": "srv", "config": {"url": "http://x"}}]}]
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert exc.value.report.reachable is False
    assert "ResponseError" in (exc.value.report.error or "")
    assert "local reload also failed" in (exc.value.report.error or "")


async def test_unconfirmed_origin_logs_error_but_returns_success(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(remotes=["serve-w1"], remote_outcome=OpOutcome.missing)
    service, _admin, _bus = _service(store, bus=bus)

    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.operations._broadcast"):
        result = await service.apply_replace({"mcp": []})

    # Persist + local reload landed, so the call SUCCEEDS; the unconfirmed origin is a
    # loud ERROR log and an explicit non-applied entry in the report.
    assert result.fleet.ok is False
    assert {r.name: r.outcome for r in result.fleet.results}["serve-w1"] == OpOutcome.missing
    assert any(record.levelno == logging.ERROR for record in caplog.records)
    assert any("did not fully converge" in record.message for record in caplog.records)


async def test_bus_unreachable_returns_success_with_unreachable_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    bus = RecordingBus(reachable=False, error="ConnectionError: bus down")
    service, admin, _bus = _service(store, bus=bus)

    result = await service.apply_replace({"mcp": []})

    # Persist + local reload landed, so the call SUCCEEDS even though the transport was
    # down: the honest bus-unreachable shape (no origin list, only an error) rides through.
    assert admin.calls == 1
    assert result.fleet.reachable is False
    assert result.fleet.error == "ConnectionError: bus down"
    assert result.fleet.results == []


# ---------------------------------------------------------------------------
# backend-needs-bus invariant — both directions
# ---------------------------------------------------------------------------


def _with_hooks_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # A registered backend also needs the shared hooks registry; the bus-rule tests
    # set it so only the bus decides their outcome.
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/0")


async def test_manifest_change_adding_backend_without_bus_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    store = FakeConfigStore(manifest={})
    service, admin, bus = _service(store)

    def add_backend(document: dict[str, Any]) -> None:
        document["backend_module"] = "myapp.backend"

    with pytest.raises(BackendNeedsBusError, match="TAI_BUS_REDIS_URL"):
        await service.apply_change(add_backend)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_manifest_change_adding_backend_with_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_hooks_store(monkeypatch)
    _with_bus(monkeypatch)
    store = FakeConfigStore(manifest={})
    service, admin, _bus = _service(store)

    def add_backend(document: dict[str, Any]) -> None:
        document["backend_module"] = "myapp.backend"

    result = await service.apply_change(add_backend)

    assert store.manifest == {"backend_module": "myapp.backend"}
    assert admin.calls == 1
    assert result.document == {"backend_module": "myapp.backend"}


async def test_env_change_materializing_backend_without_bus_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    # The manifest's backend module is an !ENV marker; the env change supplies the
    # value that materializes it — with no bus, the invariant rejects it.
    monkeypatch.delenv("TAI_BACKEND", raising=False)
    store = FakeConfigStore(manifest={"backend_module": "!ENV ${TAI_BACKEND}"})
    service, _admin, bus = _service(store)

    with pytest.raises(BackendNeedsBusError, match="TAI_BUS_REDIS_URL"):
        await service.apply_env_change({"TAI_BACKEND": "myapp.backend"})

    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_env_change_removing_bus_while_backend_present_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    # A static backend is registered and the bus is configured only through the stored
    # env; the change empties the bus var — after it, a backend would run with no bus.
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, _admin, bus = _service(store)

    with pytest.raises(BackendNeedsBusError, match="TAI_BUS_REDIS_URL"):
        await service.apply_env_change({"TAI_BUS_REDIS_URL": ""})

    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_env_change_keeping_bus_with_backend_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_hooks_store(monkeypatch)
    _no_bus(monkeypatch)
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, admin, _bus = _service(store)

    result = await service.apply_env_change({"SOME_KEY": "v"})

    assert store.env_writes == [{"SOME_KEY": "v"}]
    assert admin.calls == 1
    assert result.document is None


async def test_env_change_with_backend_and_default_namespace_bus_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bus configured ONLY through the shared TAI_DEFAULT_REDIS_URL (no
    # TAI_BUS_REDIS_URL) resolves as ENABLED through BusSettings, so an unrelated env
    # edit on a backend deployment is NOT falsely rejected (a raw TAI_BUS_REDIS_URL
    # read would have missed the default and rejected every edit).
    _no_bus(monkeypatch)
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://localhost:6379/0")
    reset_all_settings()
    try:
        store = FakeConfigStore(manifest={"backend_module": "myapp.backend"}, env={})
        service, admin, _bus = _service(store)

        result = await service.apply_env_change({"SOME_KEY": "v"})

        assert store.env_writes == [{"SOME_KEY": "v"}]
        assert admin.calls == 1
        assert result.document is None
    finally:
        reset_all_settings()


# ---------------------------------------------------------------------------
# backend-needs-hooks-store invariant — both directions
# ---------------------------------------------------------------------------


def _no_hooks_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOOKS_REDIS_URL", raising=False)
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()


async def test_manifest_change_adding_backend_without_hooks_store_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(manifest={})
    service, admin, bus = _service(store)

    def add_backend(document: dict[str, Any]) -> None:
        document["backend_module"] = "myapp.backend"

    with pytest.raises(BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        await service.apply_change(add_backend)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_change_removing_hooks_store_while_backend_present_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A static backend is registered and the hooks registry is shared only through the
    # stored env; the change empties it — after it, the backend process would match its
    # events against an empty per-process registry.
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"HOOKS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, _admin, bus = _service(store)

    with pytest.raises(BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        await service.apply_env_change({"HOOKS_REDIS_URL": ""})

    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_profile_replace_dropping_hooks_store_while_backend_present_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"HOOKS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, _admin, bus = _service(store)

    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(env)

    with pytest.raises(BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        await service.apply_replace_env({}, save_previous=save_previous)

    # Refused before anything is snapshotted or written.
    assert saved == []
    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_env_change_keeping_hooks_store_with_backend_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"HOOKS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, admin, _bus = _service(store)

    result = await service.apply_env_change({"SOME_KEY": "v"})

    assert store.env_writes == [{"SOME_KEY": "v"}]
    assert admin.calls == 1
    assert result.document is None


async def test_env_and_manifest_change_removing_hooks_store_while_backend_present_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The combined env-write + manifest-mutate door judges the same post-change env: the
    # env half empties the stored hooks registry while the manifest keeps its backend.
    _with_bus(monkeypatch)
    _no_hooks_store(monkeypatch)
    store = FakeConfigStore(
        manifest={"backend_module": "myapp.backend"},
        env={"HOOKS_REDIS_URL": "redis://localhost:6379/0"},
    )
    service, admin, bus = _service(store)

    with pytest.raises(BackendNeedsHooksStoreError, match="HOOKS_REDIS_URL"):
        await service.apply_env_and_change(_prepare_env_and_mutator({"HOOKS_REDIS_URL": ""}))

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


# ---------------------------------------------------------------------------
# workers-need-shared-stores invariant — an env change on a multi-worker server
#
# ``TAI_SERVE_WORKERS`` is the worker count ``tai serve`` stamps for its workers. Each
# store below is shared only through the stored env, and the change empties it, so every
# worker would come up on a per-process in-memory store after the change applies.
# ---------------------------------------------------------------------------

_STORE_RULES = [
    pytest.param("SUB_MCP_REDIS_URL", WorkersNeedSubMcpStoreError, id="sub-mcp"),
    pytest.param("HOOKS_REDIS_URL", WorkersNeedHooksStoreError, id="hooks"),
]


def _multi_worker_server_over_stored_stores(monkeypatch: pytest.MonkeyPatch) -> FakeConfigStore:
    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_SERVE_WORKERS", "2")
    for name in ("SUB_MCP_REDIS_URL", "HOOKS_REDIS_URL", "TAI_DEFAULT_REDIS_URL"):
        monkeypatch.delenv(name, raising=False)
    reset_all_settings()
    return FakeConfigStore(
        manifest={"mcp": []},
        env={"SUB_MCP_REDIS_URL": "redis://localhost:6379/1", "HOOKS_REDIS_URL": "redis://localhost:6379/2"},
    )


@pytest.mark.parametrize(("setting", "error"), _STORE_RULES)
async def test_env_change_removing_a_store_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch, setting: str, error: type[Exception]
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    service, admin, bus = _service(store)

    with pytest.raises(error, match=setting) as exc:
        await service.apply_env_change({setting: ""})

    assert "2 workers" in str(exc.value)
    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("setting", "error"), _STORE_RULES)
async def test_env_and_manifest_change_removing_a_store_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch, setting: str, error: type[Exception]
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    service, admin, bus = _service(store)

    with pytest.raises(error, match=setting):
        await service.apply_env_and_change(_prepare_env_and_mutator({setting: ""}))

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("setting", "error"), _STORE_RULES)
async def test_profile_replace_dropping_a_store_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch, setting: str, error: type[Exception]
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    service, _admin, bus = _service(store)
    kept = {key: value for key, value in store.env.items() if key != setting}

    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(env)

    with pytest.raises(error, match=setting):
        await service.apply_replace_env(kept, save_previous=save_previous)

    assert saved == []
    assert store.env_writes == []
    assert bus.publish_calls == []


@pytest.mark.parametrize(("setting", "error"), _STORE_RULES)
async def test_manifest_change_on_a_multi_worker_server_over_an_in_memory_store_is_rejected(
    monkeypatch: pytest.MonkeyPatch, setting: str, error: type[Exception]
) -> None:
    # A manifest change leaves the env as it is, so it is judged against the live env.
    _multi_worker_server_over_stored_stores(monkeypatch)
    monkeypatch.setenv("SUB_MCP_REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/2")
    monkeypatch.delenv(setting)
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(error, match=setting):
        await service.apply_change(add_server)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_change_keeping_the_stores_on_a_multi_worker_server_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    service, admin, _bus = _service(store)

    await service.apply_env_change({"SOME_KEY": "v"})

    assert store.env_writes == [{"SOME_KEY": "v"}]
    assert admin.calls == 1


async def test_env_change_removing_a_store_on_a_single_worker_server_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_stored_stores(monkeypatch)
    monkeypatch.setenv("TAI_SERVE_WORKERS", "1")
    service, admin, _bus = _service(store)

    await service.apply_env_change({"HOOKS_REDIS_URL": ""})

    assert store.env_writes == [{"HOOKS_REDIS_URL": ""}]
    assert admin.calls == 1


# ---------------------------------------------------------------------------
# workers-need-bus invariant — a change on a multi-worker server
#
# The bus is configured only through the stored env, and the change empties it, so every
# worker would come up on its own local bus after the change applies.
# ---------------------------------------------------------------------------

_WORKERS_NEED_BUS = r"2 workers with no worker bus.*TAI_BUS_REDIS_URL"


def _multi_worker_server_over_a_stored_bus(monkeypatch: pytest.MonkeyPatch) -> FakeConfigStore:
    _no_bus(monkeypatch)
    monkeypatch.delenv("TAI_BUS_REDIS_URL", raising=False)
    monkeypatch.setenv("TAI_SERVE_WORKERS", "2")
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    monkeypatch.setenv("SUB_MCP_REDIS_URL", "redis://localhost:6379/1")
    monkeypatch.setenv("HOOKS_REDIS_URL", "redis://localhost:6379/2")
    reset_all_settings()
    return FakeConfigStore(manifest={"mcp": []}, env={"TAI_BUS_REDIS_URL": "redis://localhost:6379/0"})


async def test_env_change_removing_the_bus_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_a_stored_bus(monkeypatch)
    service, admin, bus = _service(store)

    with pytest.raises(RuntimeError, match=_WORKERS_NEED_BUS):
        await service.apply_env_change({"TAI_BUS_REDIS_URL": ""})

    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_and_manifest_change_removing_the_bus_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_a_stored_bus(monkeypatch)
    service, admin, bus = _service(store)

    with pytest.raises(RuntimeError, match=_WORKERS_NEED_BUS):
        await service.apply_env_and_change(_prepare_env_and_mutator({"TAI_BUS_REDIS_URL": ""}))

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_profile_replace_dropping_the_bus_on_a_multi_worker_server_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_a_stored_bus(monkeypatch)
    service, _admin, bus = _service(store)

    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(env)

    with pytest.raises(RuntimeError, match=_WORKERS_NEED_BUS):
        await service.apply_replace_env({}, save_previous=save_previous)

    assert saved == []
    assert store.env_writes == []
    assert bus.publish_calls == []


async def test_manifest_change_on_a_busless_multi_worker_server_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # A manifest change leaves the env as it is, so it is judged against the live env.
    _multi_worker_server_over_a_stored_bus(monkeypatch)
    store = FakeConfigStore(manifest={"mcp": []})
    service, admin, bus = _service(store)

    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    with pytest.raises(RuntimeError, match=_WORKERS_NEED_BUS):
        await service.apply_change(add_server)

    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_change_keeping_the_bus_on_a_multi_worker_server_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _multi_worker_server_over_a_stored_bus(monkeypatch)
    service, admin, _bus = _service(store)

    await service.apply_env_change({"SOME_KEY": "v"})

    assert store.env_writes == [{"SOME_KEY": "v"}]
    assert admin.calls == 1


# ---------------------------------------------------------------------------
# post-change invariants over a working-directory env file
#
# A change is judged against the env the processes run with after it: the post-change
# stored env plus every source that stays live, the process env and a working-directory
# ``.env`` that is not the stored env file. A server whose working directory is its file-mode
# config dir reads the stored env file as that ``.env``, so the file's current contents are
# what the change replaces; any other ``.env`` keeps being read after the change.
# ---------------------------------------------------------------------------

_STORES_AND_BUS = {
    "TAI_BUS_REDIS_URL": "redis://localhost:6379/0",
    "SUB_MCP_REDIS_URL": "redis://localhost:6379/1",
    "HOOKS_REDIS_URL": "redis://localhost:6379/2",
}

_BACKEND_MANIFEST = {"backend_module": "myapp.backend"}

_POST_CHANGE_RULES = [
    pytest.param("2", {"mcp": []}, "SUB_MCP_REDIS_URL", WorkersNeedSubMcpStoreError, id="workers-sub-mcp-store"),
    pytest.param("2", {"mcp": []}, "HOOKS_REDIS_URL", WorkersNeedHooksStoreError, id="workers-hooks-store"),
    pytest.param("2", {"mcp": []}, "TAI_BUS_REDIS_URL", WorkersNeedBusError, id="workers-bus"),
    pytest.param(None, _BACKEND_MANIFEST, "TAI_BUS_REDIS_URL", BackendNeedsBusError, id="backend-bus"),
    pytest.param(None, _BACKEND_MANIFEST, "HOOKS_REDIS_URL", BackendNeedsHooksStoreError, id="backend-hooks"),
]

_SERVER_SHAPES = [
    pytest.param("2", {"mcp": []}, id="multi-worker"),
    pytest.param(None, _BACKEND_MANIFEST, id="backend"),
]


def _write_env_file(path: Path, env: dict[str, str]) -> None:
    path.write_text("".join(f"{key}={value}\n" for key, value in env.items()))


def _file_store(config_dir: Path, *, env: dict[str, str], manifest: dict[str, Any]) -> FileConfigManager:
    config_dir.mkdir(exist_ok=True)
    store = FileConfigManager(str(config_dir))
    store.write_env(env)
    store.replace_manifest(manifest)
    return store


def _server_env(monkeypatch: pytest.MonkeyPatch, *, workers: str | None, applied: dict[str, str]) -> None:
    """The process env: ``applied`` is the stored env the boot bridge applied, nothing else of the stores."""
    for key in (*_STORES_AND_BUS, "TAI_DEFAULT_REDIS_URL", "TAI_MANIFEST_PATH", "TAI_CONFIG_DIR_PATH"):
        monkeypatch.delenv(key, raising=False)
    for key, value in applied.items():
        monkeypatch.setenv(key, value)
    if workers is None:
        monkeypatch.delenv("TAI_SERVE_WORKERS", raising=False)
    else:
        monkeypatch.setenv("TAI_SERVE_WORKERS", workers)
    reset_all_settings()


def _server_in_its_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, workers: str | None, manifest: dict[str, Any]
) -> FileConfigManager:
    """A file-mode server running in its config dir: the stored env is its ``.env`` and its process env."""
    store = _file_store(tmp_path, env=_STORES_AND_BUS, manifest=manifest)
    monkeypatch.chdir(tmp_path)
    _server_env(monkeypatch, workers=workers, applied=_STORES_AND_BUS)
    return store


def _server_beside_a_working_directory_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store_kind: str,
    *,
    workers: str | None,
    manifest: dict[str, Any],
    stored: dict[str, str],
    working_dir_env: dict[str, str],
) -> FileConfigManager | FakeConfigStore:
    """A server whose working-directory ``.env`` is not its stored env file.

    ``external`` keeps the stored env in a provider store; ``file`` keeps it in a config dir
    other than the working directory.
    """
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _write_env_file(run_dir / ".env", working_dir_env)
    monkeypatch.chdir(run_dir)
    _server_env(monkeypatch, workers=workers, applied=stored)
    if store_kind == "file":
        return _file_store(tmp_path / "config", env=stored, manifest=manifest)
    return FakeConfigStore(manifest=manifest, env=dict(stored))


_STORE_KINDS = pytest.mark.parametrize("store_kind", ["external", "file"])


def _add_server(document: dict[str, Any]) -> None:
    document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]


@pytest.mark.parametrize(("workers", "manifest", "setting", "error"), _POST_CHANGE_RULES)
async def test_env_change_is_judged_without_the_stored_env_file_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workers: str | None,
    manifest: dict[str, Any],
    setting: str,
    error: type[Exception],
) -> None:
    store = _server_in_its_config_dir(monkeypatch, tmp_path, workers=workers, manifest=manifest)
    service, admin, bus = _service(store)

    with pytest.raises(error, match=setting):
        await service.apply_env_change({setting: ""})

    assert store.read_env() == _STORES_AND_BUS
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("workers", "manifest", "setting", "error"), _POST_CHANGE_RULES)
async def test_env_and_manifest_change_is_judged_without_the_stored_env_file_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workers: str | None,
    manifest: dict[str, Any],
    setting: str,
    error: type[Exception],
) -> None:
    store = _server_in_its_config_dir(monkeypatch, tmp_path, workers=workers, manifest=manifest)
    service, admin, bus = _service(store)

    with pytest.raises(error, match=setting):
        await service.apply_env_and_change(_prepare_env_and_mutator({setting: ""}))

    assert store.read_env() == _STORES_AND_BUS
    assert store.read_manifest_preserved() == manifest
    assert admin.calls == 0
    assert bus.publish_calls == []


@pytest.mark.parametrize(("workers", "manifest", "setting", "error"), _POST_CHANGE_RULES)
async def test_profile_replace_is_judged_without_the_stored_env_file_it_replaces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    workers: str | None,
    manifest: dict[str, Any],
    setting: str,
    error: type[Exception],
) -> None:
    store = _server_in_its_config_dir(monkeypatch, tmp_path, workers=workers, manifest=manifest)
    service, _admin, bus = _service(store)
    kept = {key: value for key, value in _STORES_AND_BUS.items() if key != setting}

    saved: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        saved.append(env)

    with pytest.raises(error, match=setting):
        await service.apply_replace_env(kept, save_previous=save_previous)

    assert saved == []
    assert store.read_env() == _STORES_AND_BUS
    assert bus.publish_calls == []


async def test_env_change_keeping_the_stores_and_bus_in_the_config_dir_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _server_in_its_config_dir(monkeypatch, tmp_path, workers="2", manifest=_BACKEND_MANIFEST)
    service, admin, _bus = _service(store)

    await service.apply_env_change({"SOME_KEY": "v"})

    assert store.read_env() == {**_STORES_AND_BUS, "SOME_KEY": "v"}
    assert admin.calls == 1


@_STORE_KINDS
@pytest.mark.parametrize(("workers", "manifest"), _SERVER_SHAPES)
async def test_env_change_over_a_live_working_directory_env_file_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str, workers: str | None, manifest: dict[str, Any]
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers=workers,
        manifest=manifest,
        stored={},
        working_dir_env=_STORES_AND_BUS,
    )
    service, admin, _bus = _service(store)

    await service.apply_env_change({"SOME_KEY": "v"})

    assert store.read_env() == {"SOME_KEY": "v"}
    assert admin.calls == 1


@_STORE_KINDS
@pytest.mark.parametrize(("workers", "manifest"), _SERVER_SHAPES)
async def test_env_and_manifest_change_over_a_live_working_directory_env_file_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str, workers: str | None, manifest: dict[str, Any]
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers=workers,
        manifest=manifest,
        stored={},
        working_dir_env=_STORES_AND_BUS,
    )
    service, admin, _bus = _service(store)

    await service.apply_env_and_change(_prepare_env_and_mutator({"SOME_KEY": "v"}))

    assert store.read_env() == {"SOME_KEY": "v"}
    assert admin.calls == 1


@_STORE_KINDS
@pytest.mark.parametrize(("workers", "manifest"), _SERVER_SHAPES)
async def test_profile_replace_over_a_live_working_directory_env_file_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str, workers: str | None, manifest: dict[str, Any]
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers=workers,
        manifest=manifest,
        stored={},
        working_dir_env=_STORES_AND_BUS,
    )
    service, _admin, _bus = _service(store)
    swapped: list[dict[str, str]] = []

    async def save_previous(env: dict[str, str]) -> None:
        return None

    async def build_and_swap(env: dict[str, str]) -> Epoch:
        swapped.append(env)
        return Epoch(number=0)

    await service.apply_replace_env({"SOME_KEY": "v"}, save_previous=save_previous, build_and_swap=build_and_swap)

    assert store.read_env() == {"SOME_KEY": "v"}
    assert len(swapped) == 1


@_STORE_KINDS
@pytest.mark.parametrize(("workers", "manifest"), _SERVER_SHAPES)
async def test_manifest_change_over_a_live_working_directory_env_file_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str, workers: str | None, manifest: dict[str, Any]
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers=workers,
        manifest=manifest,
        stored={},
        working_dir_env=_STORES_AND_BUS,
    )
    service, admin, _bus = _service(store)

    await service.apply_change(_add_server)

    assert store.read_manifest_preserved()["mcp"] == [{"title": "srv", "config": {"url": "http://x"}}]
    assert admin.calls == 1


_ONLY_STORED = {"SUB_MCP_REDIS_URL": _STORES_AND_BUS["SUB_MCP_REDIS_URL"]}
_WORKING_DIR_KEEPS = {key: _STORES_AND_BUS[key] for key in ("TAI_BUS_REDIS_URL", "HOOKS_REDIS_URL")}


@_STORE_KINDS
async def test_env_change_dropping_a_store_only_the_stored_env_carries_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers="2",
        manifest={"mcp": []},
        stored=_ONLY_STORED,
        working_dir_env=_WORKING_DIR_KEEPS,
    )
    service, admin, _bus = _service(store)

    with pytest.raises(WorkersNeedSubMcpStoreError, match="SUB_MCP_REDIS_URL"):
        await service.apply_env_change({"SUB_MCP_REDIS_URL": ""})

    assert store.read_env() == _ONLY_STORED
    assert admin.calls == 0


@_STORE_KINDS
async def test_env_and_manifest_change_dropping_a_store_only_the_stored_env_carries_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers="2",
        manifest={"mcp": []},
        stored=_ONLY_STORED,
        working_dir_env=_WORKING_DIR_KEEPS,
    )
    service, admin, _bus = _service(store)

    with pytest.raises(WorkersNeedSubMcpStoreError, match="SUB_MCP_REDIS_URL"):
        await service.apply_env_and_change(_prepare_env_and_mutator({"SUB_MCP_REDIS_URL": ""}))

    assert store.read_env() == _ONLY_STORED
    assert admin.calls == 0


@_STORE_KINDS
async def test_profile_replace_dropping_a_store_only_the_stored_env_carries_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str
) -> None:
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers="2",
        manifest={"mcp": []},
        stored=_ONLY_STORED,
        working_dir_env=_WORKING_DIR_KEEPS,
    )
    service, admin, _bus = _service(store)

    async def save_previous(env: dict[str, str]) -> None:
        return None

    with pytest.raises(WorkersNeedSubMcpStoreError, match="SUB_MCP_REDIS_URL"):
        await service.apply_replace_env({}, save_previous=save_previous)

    assert store.read_env() == _ONLY_STORED
    assert admin.calls == 0


@_STORE_KINDS
async def test_env_change_dropping_a_store_the_working_directory_env_file_keeps_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store_kind: str
) -> None:
    # The working-directory file keeps the hooks registry shared after the stored copy goes.
    stored = {"HOOKS_REDIS_URL": _STORES_AND_BUS["HOOKS_REDIS_URL"]}
    store = _server_beside_a_working_directory_env_file(
        monkeypatch,
        tmp_path,
        store_kind,
        workers="2",
        manifest={"mcp": []},
        stored=stored,
        working_dir_env=_STORES_AND_BUS,
    )
    service, admin, _bus = _service(store)

    await service.apply_env_change({"HOOKS_REDIS_URL": ""})

    assert not store.read_env().get("HOOKS_REDIS_URL")
    assert admin.calls == 1


# ---------------------------------------------------------------------------
# apply-window invariant — the window covers three drain budgets after the change
# ---------------------------------------------------------------------------


async def test_env_change_raising_the_drain_past_the_apply_window_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_skeleton.app.boot_rules import ApplyWindowBelowDrainError

    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "90")
    monkeypatch.delenv("TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS", raising=False)
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, bus = _service(store)

    with pytest.raises(ApplyWindowBelowDrainError) as exc:
        await service.apply_env_change({"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"})

    message = str(exc.value)
    assert "TAI_BUS_APPLY_TIMEOUT=90" in message
    assert "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90" in message
    assert "270" in message
    assert store.env_writes == []
    assert admin.calls == 0
    assert bus.publish_calls == []


async def test_env_change_raising_the_drain_within_a_wide_apply_window_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "270")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, _bus = _service(store)

    await service.apply_env_change({"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"})

    assert store.env_writes == [{"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"}]
    assert admin.calls == 1


async def test_env_change_without_a_bus_never_applies_the_window_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_bus(monkeypatch)
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "5")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, _bus = _service(store)

    await service.apply_env_change({"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"})

    assert store.env_writes == [{"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"}]
    assert admin.calls == 1


def _prepare_env_and_mutator(
    changes: dict[str, str],
) -> Callable[[dict[str, str]], Awaitable[tuple[dict[str, str], Callable[[dict[str, Any]], None]]]]:
    def add_server(document: dict[str, Any]) -> None:
        document["mcp"] = [{"title": "srv", "config": {"url": "http://x"}}]

    async def prepare(_stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        return changes, add_server

    return prepare


async def test_env_and_manifest_change_raising_the_drain_past_the_apply_window_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tai42_skeleton.app.boot_rules import ApplyWindowBelowDrainError

    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_BUS_APPLY_TIMEOUT", "90")
    reset_all_settings()
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, admin, bus = _service(store)

    with pytest.raises(ApplyWindowBelowDrainError, match="270"):
        await service.apply_env_and_change(_prepare_env_and_mutator({"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": "90"}))

    assert store.env_writes == []
    assert store.persisted == []
    assert admin.calls == 0
    assert bus.publish_calls == []
