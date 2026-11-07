"""Pipeline runs inside a :func:`deferring_rolls` scope record their reload; the scope's caller decides once."""

from __future__ import annotations

from typing import Any

import pytest

import tai42_skeleton.backend.settings  # noqa: F401  — declares BACKEND_MANIFEST_KEY
from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerIdentity, WorkerKind, WorkerResult
from tai42_skeleton.app.writer_exit import WriterExitScope
from tai42_skeleton.config.service import DeferredRolls, deferring_rolls
from tai42_skeleton.config.service.deferred_roll import DeferredReload
from tai42_skeleton.operations._broadcast import FleetBroadcastError

from .._fakes.bus import FakeBus, _row
from .fake_pipeline import (
    FakeConfigStore,
    FakeReloadAdmin,
    _orchestrate_spy,
    _reset_settings_after,  # noqa: F401  — autouse fixture, active on import
    _service,
    _with_bus,
    discarded_build_failure,
)

# The door's edge settles the writer's exit its change owes; these tests drive the door below it.
pytestmark = pytest.mark.usefixtures("writer_exit_edge")

_KEY = "BACKEND_MANIFEST_KEY"
_MOVED = {"status": "ok", "env_keys": 1, "awaiting_respawn": [_KEY]}
_SETTLED = {"status": "ok", "env_keys": 1, "awaiting_respawn": []}
_WRITER = WorkerIdentity(name="serve-writer", kind=WorkerKind.serve, pid=1, generation=1)


def _writer_bus(*, moved: tuple[str, ...] = ()) -> FakeBus:
    """The writer's bus with one sibling, ``serve-b``; each name in ``moved`` reports the moved key."""
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    for name in moved:
        bus.remote_payloads[name] = dict(_MOVED)
    return bus


def _counting(orch: Any) -> tuple[Any, list[int]]:
    calls: list[int] = []

    async def counted(bus: Any, **kwargs: Any) -> Any:
        calls.append(1)
        return await orch(bus, **kwargs)

    return counted, calls


async def test_a_run_inside_the_scope_records_its_reload_instead_of_deciding(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    store = FakeConfigStore(manifest={"mcp": []}, env={_KEY: "OLD"})
    service, _admin, _bus = _service(store, bus=_writer_bus(moved=("serve-b",)), orchestrate=counted)

    with deferring_rolls() as deferral:
        result = await service.apply_env_change({_KEY: "NEW"})

    assert calls == []
    assert store.env == {_KEY: "NEW"}
    assert result.roll_deferred is True
    assert result.decision is None
    assert result.recycle is None
    assert result.awaiting_respawn == []
    [recorded] = deferral.reloads
    assert recorded.writer.name == "serve-writer"
    assert recorded.pinned == {"serve-b": (WorkerKind.serve, 1)}
    assert recorded.report is result.fleet


async def test_the_scope_decides_once_from_its_latest_reload_over_every_pinned_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    bus = _writer_bus(moved=("serve-b", "serve-c"))
    store = FakeConfigStore(manifest={"mcp": []}, env={_KEY: "OLD"})
    service, _admin, _bus = _service(store, admin=FakeReloadAdmin(result=dict(_MOVED)), bus=bus, orchestrate=counted)

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
    assert seen["decision"].writer.name == "serve-writer"
    assert seen["decision"].clean == set()
    assert seen["applier_self_deferred"] is True
    assert outcome is not None
    assert set(outcome.decision.reported) == {"serve-b", "serve-c"}
    folded = first.rolled(outcome)
    assert folded.roll_deferred is False
    assert folded.recycle is outcome.recycle
    assert folded.self_exit_armed is True


async def test_a_scope_with_no_recorded_reload_decides_nothing() -> None:
    service, _admin, _bus = _service(FakeConfigStore(manifest={"mcp": []}, env={}))

    assert await service.run_deferred_rolls(DeferredRolls(), writer_exits=True, change="the test change") is None


async def test_a_scope_whose_fleet_confirmed_clean_rolls_nothing_and_logs_the_skip(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    store = FakeConfigStore(manifest={"mcp": []}, env={})
    service, _admin, _bus = _service(store, bus=_writer_bus(), orchestrate=counted)
    caplog.set_level("INFO")

    with deferring_rolls() as deferral:
        result = await service.apply_env_change({"NEW_KEY": "v"})
    outcome = await service.run_deferred_rolls(deferral, writer_exits=True, change="the test change")

    assert calls == []
    assert outcome is not None
    assert outcome.recycle is None
    assert outcome.decision.clean == {"serve-b"}
    assert result.rolled(outcome).awaiting_respawn == []
    assert "the fleet recycle for the test change is skipped" in caplog.text


async def test_a_run_whose_broadcast_raised_keeps_the_writer_statement_and_the_scope_asks_the_pinned_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy()
    counted, calls = _counting(orch)

    class _RaisingBus(FakeBus):
        async def publish(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("bus refused the publish")

    store = FakeConfigStore(manifest={"mcp": []}, env={_KEY: "OLD"})
    service, _admin, _bus = _service(
        store, bus=_RaisingBus(origin="serve-writer", remotes=["serve-b"]), orchestrate=counted
    )

    with deferring_rolls() as deferral, pytest.raises(FleetBroadcastError):
        await service.apply_env_change({_KEY: "NEW"})
    outcome = await service.run_deferred_rolls(deferral, writer_exits=True, change="the failed op")

    assert calls == [1]
    assert seen["expected"] == {"serve-b": (WorkerKind.serve, 1)}
    assert outcome is not None
    assert set(outcome.decision.unconfirmed) == {"serve-b"}
    # The writer's own applied reload stated no drift: the recorded report keeps it, clean.
    assert outcome.decision.writer_reason is None
    assert outcome.self_exit_armed is False


async def test_a_run_whose_writer_build_was_refused_and_whose_broadcast_raised_records_the_writer_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The recorded report keeps the writer's own refused build: the scope's roll asks the pinned
    # sibling, whose state is unknown, once it is ready, and never restarts the writer.
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)

    class _RaisingBus(FakeBus):
        async def publish(self, *args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("bus refused the publish")

    refusal = await discarded_build_failure(RuntimeError("chain pending: run tai db migrate"))
    store = FakeConfigStore(manifest={"mcp": []}, env={_KEY: "OLD"})
    service, _admin, _bus = _service(
        store,
        admin=FakeReloadAdmin(raise_reload=refusal),
        bus=_RaisingBus(origin="serve-writer", remotes=["serve-b"]),
        orchestrate=counted,
    )

    with deferring_rolls() as deferral, pytest.raises(FleetBroadcastError) as exc:
        await service.apply_env_change({_KEY: "NEW"})
    outcome = await service.run_deferred_rolls(deferral, writer_exits=True, change="the failed op")

    assert exc.value.report.results == []
    assert calls == [1]
    assert outcome is not None
    assert set(outcome.decision.unconfirmed) == {"serve-b"}
    assert outcome.decision.writer_reason is None
    assert not outcome.decision.writer_needs
    assert outcome.self_exit_armed is False


async def test_a_roll_with_no_writer_exit_lists_the_writer_in_the_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy()
    store = FakeConfigStore(manifest={"mcp": []}, env={_KEY: "OLD"})
    service, _admin, _bus = _service(
        store, admin=FakeReloadAdmin(result=dict(_MOVED)), bus=_writer_bus(moved=("serve-b",)), orchestrate=orch
    )

    with deferring_rolls() as deferral:
        await service.apply_env_change({_KEY: "NEW"})
    outcome = await service.run_deferred_rolls(deferral, writer_exits=False, change="the test change")

    assert seen["applier_self_deferred"] is False
    assert outcome is not None
    assert outcome.self_exit_armed is False
    # The writer stated the moved key and no self-exit follows, so it still awaits a respawn.
    assert [(worker.name, worker.keys) for worker in outcome.awaiting_respawn] == [("serve-writer", [_KEY])]


async def test_a_reload_that_continues_a_deferred_run_records_its_reload_too(monkeypatch: pytest.MonkeyPatch) -> None:
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
    assert [reload.report for reload in deferral.reloads] == [first.fleet, continued.fleet]
    merged = deferral.merged()
    assert merged is not None
    assert merged.report is continued.fleet


def _reload_report(
    *, writer: dict[str, Any], sibling: dict[str, Any] | None, sibling_outcome: OpOutcome = OpOutcome.applied
) -> FleetResult:
    """The scope's latest reload: the writer's row and serve-b's (none when ``sibling`` is ``None``)."""
    rows = [WorkerResult(name="serve-writer", outcome=OpOutcome.applied, payload=writer)]
    if sibling is not None:
        rows.append(WorkerResult(name="serve-b", outcome=sibling_outcome, payload=sibling))
    return FleetResult(op="reload_config", results=rows)


def _deferred(report: FleetResult) -> DeferredRolls:
    """A scope whose one run pinned serve-b, its reload ``report``."""
    return DeferredRolls(
        reloads=[DeferredReload(writer=_WRITER, pinned={"serve-b": (WorkerKind.serve, 1)}, report=report)]
    )


async def test_a_roll_no_target_needs_is_skipped_when_the_writer_does_not_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    service, _admin, _bus = _service(FakeConfigStore(manifest={"mcp": []}, env={}), orchestrate=counted)
    caplog.set_level("INFO")

    # Every sibling confirms it is clean; the writer still states a moved key.
    deferral = _deferred(_reload_report(writer=_MOVED, sibling=_SETTLED))
    outcome = await service.run_deferred_rolls(deferral, writer_exits=False, change="the failed op")

    assert calls == []
    assert outcome is not None
    assert outcome.recycle is None
    assert outcome.decision.writer == _WRITER
    # The writer the roll never restarts is still listed and logged as awaiting a respawn.
    assert [(worker.name, worker.keys) for worker in outcome.awaiting_respawn] == [("serve-writer", [_KEY])]
    assert (
        "the fleet recycle for the failed op is skipped: only the writer 'serve-writer' needs a restart, which no "
        "exit follows (confirmed clean: serve-b; left running, its restart withheld: none)" in caplog.text
    )
    assert "no worker needs a restart" not in caplog.text


@pytest.mark.parametrize(
    ("sibling", "sibling_outcome"),
    [
        pytest.param(_MOVED, OpOutcome.applied, id="the-target-states-a-moved-key"),
        pytest.param(_SETTLED, OpOutcome.failed, id="the-target-did-not-confirm-the-reload"),
        pytest.param(None, OpOutcome.applied, id="the-target-is-missing-from-the-reload"),
    ],
)
async def test_a_roll_a_target_needs_runs_when_the_writer_does_not_exit(
    monkeypatch: pytest.MonkeyPatch, sibling: dict[str, Any] | None, sibling_outcome: OpOutcome
) -> None:
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    service, _admin, _bus = _service(FakeConfigStore(manifest={"mcp": []}, env={}), orchestrate=counted)

    deferral = _deferred(_reload_report(writer=_SETTLED, sibling=sibling, sibling_outcome=sibling_outcome))
    outcome = await service.run_deferred_rolls(deferral, writer_exits=False, change="the failed op")

    assert calls == [1]
    assert outcome is not None
    assert outcome.recycle is not None
    assert outcome.decision.rolls_over("serve-b")


async def test_a_roll_runs_on_an_unreachable_reload_when_the_writer_does_not_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    service, _admin, _bus = _service(FakeConfigStore(manifest={"mcp": []}, env={}), orchestrate=counted)

    deferral = _deferred(FleetResult(op="reload_config", reachable=False, error="bus gone"))
    await service.run_deferred_rolls(deferral, writer_exits=False, change="the failed op")

    assert calls == [1]


async def test_a_roll_whose_writer_exits_runs_though_no_target_needs_it(
    monkeypatch: pytest.MonkeyPatch, writer_exit_edge: WriterExitScope
) -> None:
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy()
    counted, calls = _counting(orch)
    service, _admin, bus = _service(FakeConfigStore(manifest={"mcp": []}, env={}), orchestrate=counted)

    # The op's edge holds the writer's exit from the change's start; the roll converts that hold.
    writer_exit_edge.hold_exit(bus, "the op")
    # The writer's post-reply exit is armed only by a converged roll, so the roll runs.
    deferral = _deferred(_reload_report(writer=_MOVED, sibling=_SETTLED))
    outcome = await service.run_deferred_rolls(deferral, writer_exits=True, change="the op")

    assert calls == [1]
    assert outcome is not None
    assert outcome.self_exit_armed is True
