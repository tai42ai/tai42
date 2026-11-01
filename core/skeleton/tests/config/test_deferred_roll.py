"""Pipeline runs inside a :func:`deferring_rolls` scope record their roll; the scope's caller runs them as one."""

from __future__ import annotations

from typing import Any

import pytest

import tai42_skeleton.backend.settings  # noqa: F401  — declares BACKEND_MANIFEST_KEY
from tai42_skeleton.app.bus import WorkerKind
from tai42_skeleton.config.service import deferring_rolls

from .._fakes.bus import FakeBus, _row
from .fake_pipeline import (
    FakeConfigStore,
    FakeReloadAdmin,
    _orchestrate_spy,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
)

_KEY = "BACKEND_MANIFEST_KEY"
_MOVED = {"status": "ok", "env_keys": 1, "awaiting_respawn": [_KEY]}


def _writer_bus() -> FakeBus:
    return FakeBus(origin="serve-writer", remotes=["serve-b"])


def _counting(orch: Any) -> tuple[Any, list[int]]:
    calls: list[int] = []

    async def counted(bus: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await orch(bus, **kwargs)

    return counted, calls


async def test_a_run_inside_the_scope_records_its_roll_instead_of_rolling(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    store = FakeConfigStore(manifest={"mcp": []}, env={_KEY: "OLD"})
    service, _admin, _bus = _service(store, bus=_writer_bus(), orchestrate=counted)

    with deferring_rolls() as deferral:
        result = await service.apply_env_change({_KEY: "NEW"})

    assert calls == []
    assert store.env == {_KEY: "NEW"}
    assert result.roll_deferred is True
    assert result.recycle is None
    assert result.awaiting_respawn == []
    assert [roll.pinned for roll in deferral.rolls] == [{"serve-b": (WorkerKind.serve, 1)}]
    assert deferral.last_fleet is result.fleet


async def test_the_deferred_rolls_run_as_one_over_every_pinned_target(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    store = FakeConfigStore(manifest={"mcp": []}, env={_KEY: "OLD"})
    service, _admin, _bus = _service(store, bus=bus, orchestrate=counted)

    with deferring_rolls() as deferral:
        first = await service.apply_env_change({_KEY: "MID"})
        # A sibling joins and serve-b moves to a later life between the two changes.
        bus._remote_rows = [
            _row("serve-b", WorkerKind.serve, pid=2, generation=4, pttl_ms=15000),
            _row("serve-c", WorkerKind.serve, pid=3, generation=1, pttl_ms=15000),
        ]
        await service.apply_env_change({_KEY: "NEW"})
    outcome = await service.run_deferred_rolls(deferral, writer_exits=True, change="the test change")

    assert calls == [1]
    assert seen["expected"] == {"serve-b": (WorkerKind.serve, 4), "serve-c": (WorkerKind.serve, 1)}
    assert seen["excluded_name"] == "serve-writer"
    assert seen["applier_self_deferred"] is True
    assert outcome is not None
    assert [row.name for row in outcome.recycle.rows] == ["serve-b"]
    folded = first.rolled(outcome)
    assert folded.roll_deferred is False
    assert folded.self_exit_armed is True


async def test_a_scope_that_deferred_no_roll_runs_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, _admin, _bus = _service(store, bus=_writer_bus(), orchestrate=counted)

    with deferring_rolls() as deferral:
        result = await service.apply_env_change({"NEW_KEY": "v"})

    assert await service.run_deferred_rolls(deferral, writer_exits=True, change="the test change") is None
    assert calls == []
    assert result.roll_deferred is False
    assert result.rolled(None) is result


async def test_a_roll_with_no_writer_exit_lists_the_writer_in_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy()
    store = FakeConfigStore(manifest={"mcp": []}, env={_KEY: "OLD"})
    service, _admin, _bus = _service(
        store,
        admin=FakeReloadAdmin(result=_MOVED),
        bus=FakeBus(origin="serve-writer", remotes=["serve-b"]),
        orchestrate=orch,
    )

    with deferring_rolls() as deferral:
        await service.apply_env_change({_KEY: "NEW"})
    outcome = await service.run_deferred_rolls(deferral, writer_exits=False, change="the test change")

    assert seen["applier_self_deferred"] is False
    assert outcome is not None
    # The writer stated the moved key and no self-exit follows, so it still awaits a respawn.
    assert [(worker.name, worker.keys) for worker in outcome.awaiting_respawn] == [("serve-writer", [_KEY])]


async def test_a_reload_that_continues_a_deferred_run_carries_the_deferral(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    store = FakeConfigStore(manifest={"mcp": []}, env={_KEY: "OLD"})
    service, _admin, _bus = _service(store, bus=_writer_bus(), orchestrate=counted)

    with deferring_rolls() as deferral:
        first = await service.apply_env_change({_KEY: "NEW"})
        continued = await service.reapply_manifest(continues=first)

    assert calls == []
    assert continued.roll_deferred is True
    assert continued.self_identity == first.self_identity
    assert len(deferral.rolls) == 1
    assert deferral.last_fleet is continued.fleet
