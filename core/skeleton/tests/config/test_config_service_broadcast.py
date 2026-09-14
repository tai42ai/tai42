"""Unit oracles for :class:`~tai42_skeleton.config.service.ConfigService` — the post-persist
reload + broadcast tail and its failure discipline: a local reload that fails after the
persist landed still broadcasts and then re-raises with the fleet report; a raw broadcast
fault becomes a :class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError` carrying
the honest bus-unreachable shape; an unconfirmed origin is a loud ERROR log but a returned
success; expected membership is pinned to op start, not publish time; and the
backend-needs-bus invariant rejects both directions.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.app.bus import OpOutcome
from tai42_skeleton.operations._broadcast import FleetBroadcastError

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
