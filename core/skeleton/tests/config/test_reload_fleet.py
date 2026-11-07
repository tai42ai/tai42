"""The fleet soft restart (``reload_fleet``) restarts the drifted workers whose reload built.

The reload decides through the same recycle decision as every config write: an addressed worker
whose reload built and reports a recycle-class key off its bound value, or whose state is unknown,
is restarted; a worker whose reload was refused at its epoch build is left running, its restart
withheld and named in the verdict, even when it reports keys; a worker that confirmed clean, or
that a reload naming its targets did not address, is left running.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from tai42_kit.settings import bind_respawn_class_env
from tai42_kit.utils.run_bound import bounded_run

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, WorkerRow
from tai42_skeleton.app.discarded_build import DISCARDED_BUILD_STATUS
from tai42_skeleton.app.recycle import RECYCLED, RecycleReport, RecycleRow
from tai42_skeleton.config.recycle_policy import REFUSED_BUILD, RecycleReason
from tai42_skeleton.operations._broadcast import FleetBroadcastError

from .._fakes.bus import FakeBus
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

_MOVED = {"status": "ok", "env_keys": 1, "awaiting_respawn": ["BACKEND_MANIFEST_KEY"]}
_CLEAN = {"status": "ok", "env_keys": 1, "awaiting_respawn": []}


def _fleet(**payloads: dict[str, Any]) -> FakeBus:
    bus = FakeBus(origin="serve-writer", remotes=sorted(payloads))
    bus.remote_payloads.update(payloads)
    return bus


async def test_a_whole_fleet_reload_restarts_the_drifted_and_leaves_the_clean_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy()
    service, admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(result=dict(_CLEAN)),
        bus=_fleet(**{"serve-b": _MOVED, "serve-c": _CLEAN}),
        orchestrate=orch,
    )

    outcome = await service.reload_fleet(None, change="the fleet soft restart")

    assert admin.calls == 1
    assert seen["decision"].clean == {"serve-c"}
    assert seen["applier_self_deferred"] is False
    assert outcome.roll.decision.reported.keys() == {"serve-b"}
    assert outcome.roll.self_exit_armed is False
    assert outcome.roll.awaiting_respawn == []


async def test_a_whole_fleet_reload_whose_writer_drifted_arms_the_writer_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy(RecycleReport())
    service, _admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(result=dict(_MOVED)),
        bus=_fleet(**{"serve-b": _CLEAN}),
        orchestrate=orch,
    )

    outcome = await service.reload_fleet(None, change="the fleet soft restart")

    assert seen["decision"].clean == {"serve-b"}
    assert seen["applier_self_deferred"] is True
    assert outcome.roll.self_exit_armed is True
    assert outcome.roll.awaiting_respawn == []


async def test_a_targeted_reload_leaves_the_workers_it_did_not_address_running(monkeypatch: pytest.MonkeyPatch) -> None:
    # serve-c is drifted but the reload names only serve-b: serve-c is not addressed, so it is
    # left running, and the writer, not addressed either, is not judged and reloads nothing.
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy(
        RecycleReport(rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED)])
    )
    service, admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(result=dict(_MOVED)),
        bus=_fleet(**{"serve-b": _MOVED, "serve-c": _MOVED}),
        orchestrate=orch,
    )

    outcome = await service.reload_fleet(["serve-b"], change="the fleet soft restart")

    assert admin.calls == 0
    assert seen["decision"].addressed == {"serve-b"}
    assert not seen["decision"].rolls_over("serve-c")
    assert seen["applier_self_deferred"] is False
    assert outcome.roll.decision.reported.keys() == {"serve-b"}
    assert outcome.roll.decision.writer_reason is None
    assert outcome.roll.self_exit_armed is False


async def test_a_targeted_reload_of_clean_workers_rolls_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy()
    service, _admin, _bus = _service(
        FakeConfigStore(), bus=_fleet(**{"serve-b": _CLEAN, "serve-c": _MOVED}), orchestrate=orch
    )

    outcome = await service.reload_fleet(["serve-b"], change="the fleet soft restart")

    assert seen == {}
    assert outcome.roll.recycle is None
    assert outcome.roll.decision.clean == {"serve-b"}
    assert not outcome.roll.decision.rolls_over("serve-c")


class _CensusDownBus(FakeBus):
    """A :class:`FakeBus` whose census cannot be read, so the reload's op-start census fails."""

    async def census(self) -> list[WorkerRow]:
        raise ConnectionError("bus census unreachable")


@pytest.mark.parametrize(
    ("targets", "without"),
    [
        (
            None,
            "the publish-time census stands in for the membership (a worker whose presence fades during the "
            "local apply may go unreported) and no recycle target is pinned",
        ),
        (["serve-b"], "no recycle target is pinned"),
    ],
)
async def test_a_reload_whose_op_start_census_fails_says_what_it_goes_on_without(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, targets: list[str] | None, without: str
) -> None:
    # A reload naming its targets takes no op-start membership, so a failed read costs it only the pin.
    _with_bus(monkeypatch)
    bus = _CensusDownBus(origin="serve-writer", remotes=["serve-b"])
    bus.remote_payloads["serve-b"] = dict(_CLEAN)
    orch, _seen = _orchestrate_spy()
    service, _admin, _bus = _service(
        FakeConfigStore(), admin=FakeReloadAdmin(result=dict(_CLEAN)), bus=bus, orchestrate=orch
    )

    with caplog.at_level(logging.WARNING, logger="tai42_skeleton.operations._broadcast"):
        await service.reload_fleet(targets, change="the fleet soft restart")

    [warning] = [r.getMessage() for r in caplog.records if "op-start census" in r.getMessage()]
    assert warning == f"worker bus: op-start census for 'reload_config' failed — {without}"
    assert bus.expected_at_start_calls == [None]


async def test_a_reload_whose_own_reload_failed_still_restarts_the_drifted_and_raises_with_the_roll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The writer's own reload failed, so its state is unknown and its own restart follows the
    # roll; serve-b reports a key and is restarted, serve-c confirmed clean and is left running.
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy(
        RecycleReport(rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED)])
    )
    service, _admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(raise_reload=RuntimeError("reload boom")),
        bus=_fleet(**{"serve-b": _MOVED, "serve-c": _CLEAN}),
        orchestrate=orch,
    )

    with pytest.raises(FleetBroadcastError, match="reload boom") as exc:
        await service.reload_fleet(None, change="the fleet soft restart")

    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.reported.keys() == {"serve-b"}
    assert roll.decision.clean == {"serve-c"}
    assert roll.decision.writer_reason == RecycleReason(unknown="its reload reported failed")
    assert seen["applier_self_deferred"] is True
    assert roll.self_exit_armed is True
    assert roll.awaiting_respawn == []


async def test_a_reload_refused_at_the_writers_epoch_build_raises_and_leaves_the_writer_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The writer's build was discarded before its swap and measured no drift after the discard: no
    # restart of its own follows; serve-b reports a key and is restarted, serve-c confirmed clean and
    # is left running; the refusal still raises.
    _with_bus(monkeypatch)
    orch, _seen = _orchestrate_spy(
        RecycleReport(rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED)])
    )
    refusal = await discarded_build_failure(RuntimeError("chain pending: run tai db migrate"))
    service, _admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(raise_reload=refusal),
        bus=_fleet(**{"serve-b": _MOVED, "serve-c": _CLEAN}),
        orchestrate=orch,
    )

    with pytest.raises(FleetBroadcastError, match="chain pending") as exc:
        await service.reload_fleet(None, change="the fleet soft restart")

    assert exc.value.__cause__ is refusal
    [writer_row] = [row for row in exc.value.report.results if row.name == "serve-writer"]
    assert writer_row.payload == {"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": []}
    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.writer_reason is None
    assert roll.decision.reported.keys() == {"serve-b"}
    assert roll.decision.clean == {"serve-c"}
    assert roll.self_exit_armed is False


async def test_a_writer_refused_at_its_epoch_build_still_owing_a_respawn_is_named_never_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An earlier change moved a recycle-class key the writer is still bound under. Its build of the
    # stored config failed and its exit may fail the same way: none is armed, and the verdict names
    # the writer with the key.
    import tai42_skeleton.app.bus_settings  # noqa: F401  — registers the recycle-class bus group

    _with_bus(monkeypatch)
    monkeypatch.setenv("TAI_BUS_HEARTBEAT_TTL", "15")
    bind_respawn_class_env()
    monkeypatch.setenv("TAI_BUS_HEARTBEAT_TTL", "9")
    orch, _seen = _orchestrate_spy(RecycleReport())
    refusal = await discarded_build_failure(RuntimeError("chain pending: run tai db migrate"))
    service, _admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(raise_reload=refusal),
        bus=_fleet(**{"serve-b": _CLEAN}),
        orchestrate=orch,
    )

    with pytest.raises(FleetBroadcastError, match="chain pending") as exc:
        await service.reload_fleet(None, change="the fleet soft restart")

    [writer_row] = [row for row in exc.value.report.results if row.name == "serve-writer"]
    assert writer_row.payload == {"status": DISCARDED_BUILD_STATUS, "awaiting_respawn": ["TAI_BUS_HEARTBEAT_TTL"]}
    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.writer_reason == RecycleReason(keys=("TAI_BUS_HEARTBEAT_TTL",), withheld=REFUSED_BUILD)
    assert not roll.decision.writer_needs
    assert roll.self_exit_armed is False
    assert [(worker.name, worker.keys) for worker in roll.awaiting_respawn] == [
        ("serve-writer", ["TAI_BUS_HEARTBEAT_TTL"])
    ]


class _UnreachableFleet(FakeBus):
    """A bus whose reload broadcast hits a transport fault: the report holds no worker row."""

    async def publish(self, op: Any, targets: Any, local: Any, *, expected_at_start: Any = None) -> FleetResult:
        await super().publish(op, targets, local, expected_at_start=expected_at_start)
        return FleetResult(op=op.op, reachable=False, error="ConnectionError: bus down")


async def _writer_owing_a_respawn(monkeypatch: pytest.MonkeyPatch) -> Exception:
    """The writer bound under one heartbeat TTL and the env now naming another; its reload refused."""
    import tai42_skeleton.app.bus_settings  # noqa: F401  — registers the recycle-class bus group

    monkeypatch.setenv("TAI_BUS_HEARTBEAT_TTL", "15")
    bind_respawn_class_env()
    monkeypatch.setenv("TAI_BUS_HEARTBEAT_TTL", "9")
    return await discarded_build_failure(RuntimeError("chain pending: run tai db migrate"))


async def test_a_writer_refused_at_its_epoch_build_is_withheld_when_its_broadcast_misses_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The broadcast's report lost the writer's own row, yet the writer knows its build was refused:
    # the roll that follows in the request (its orchestration a spy here) withholds the writer's
    # restart and arms no exit.
    _with_bus(monkeypatch)
    refusal = await _writer_owing_a_respawn(monkeypatch)
    orch, seen = _orchestrate_spy(RecycleReport())
    service, _admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(raise_reload=refusal),
        bus=_UnreachableFleet(origin="serve-writer", remotes=["serve-b"]),
        orchestrate=orch,
    )

    with pytest.raises(FleetBroadcastError, match="chain pending") as exc:
        await service.reload_fleet(None, change="the fleet soft restart")

    assert exc.value.report.reachable is False
    assert exc.value.report.results == []
    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.writer_reason == RecycleReason(keys=("TAI_BUS_HEARTBEAT_TTL",), withheld=REFUSED_BUILD)
    assert seen["applier_self_deferred"] is False
    assert roll.self_exit_armed is False
    assert [(worker.name, worker.keys) for worker in roll.awaiting_respawn] == [
        ("serve-writer", ["TAI_BUS_HEARTBEAT_TTL"])
    ]


async def test_a_writer_whose_reload_built_clean_keeps_running_when_its_broadcast_misses_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The report holds no row for the writer, yet its own applied reload stated no drift: the roll
    # that follows in the request (its orchestration a spy here) asks the siblings and leaves the
    # writer running.
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy(RecycleReport())
    service, _admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(result=dict(_CLEAN)),
        bus=_UnreachableFleet(origin="serve-writer", remotes=["serve-b"]),
        orchestrate=orch,
    )

    outcome = await service.reload_fleet(None, change="the fleet soft restart")

    assert outcome.fleet.reachable is False
    assert outcome.fleet.results == []
    assert outcome.roll.decision.writer_reason is None
    assert seen["applier_self_deferred"] is False
    assert outcome.roll.self_exit_armed is False


async def test_a_writer_whose_reload_built_and_drifted_is_restarted_when_its_broadcast_misses_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy(RecycleReport())
    service, _admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(result=dict(_MOVED)),
        bus=_UnreachableFleet(origin="serve-writer", remotes=["serve-b"]),
        orchestrate=orch,
    )

    outcome = await service.reload_fleet(None, change="the fleet soft restart")

    assert outcome.roll.decision.writer_reason == RecycleReason(keys=("BACKEND_MANIFEST_KEY",))
    assert seen["applier_self_deferred"] is True
    assert outcome.roll.self_exit_armed is True


async def test_a_writer_whose_reload_failed_otherwise_is_restarted_when_its_broadcast_misses_the_bus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failure that states nothing of the writer's env leaves its state unknown: the roll restarts it.
    _with_bus(monkeypatch)
    orch, seen = _orchestrate_spy(RecycleReport())
    service, _admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(raise_reload=RuntimeError("reload handler crashed")),
        bus=_UnreachableFleet(origin="serve-writer", remotes=["serve-b"]),
        orchestrate=orch,
    )

    with pytest.raises(FleetBroadcastError, match="reload handler crashed") as exc:
        await service.reload_fleet(None, change="the fleet soft restart")

    roll = exc.value.roll
    assert roll is not None
    assert roll.decision.writer_reason is not None
    assert roll.decision.writer_reason.unknown is not None
    assert seen["applier_self_deferred"] is True
    assert roll.self_exit_armed is True


async def test_a_soft_restart_whose_roll_hand_off_fails_raises_what_the_roll_layer_knows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The soft restart writes nothing: its failed hand-off says only whether the roll started
    # and whether the reload reached the bus, never that a change persisted.
    _with_bus(monkeypatch)
    service, _admin, _bus = _service(
        FakeConfigStore(), admin=FakeReloadAdmin(result=dict(_CLEAN)), bus=_fleet(**{"serve-b": _MOVED})
    )
    monkeypatch.setattr(instance, "_app", None)

    async with bounded_run(60):
        with pytest.raises(RuntimeError) as raised:
            await service.reload_fleet(None, change="the fleet soft restart")

    assert not isinstance(raised.value, FleetBroadcastError)
    assert str(raised.value) == (
        "fleet recycle: the roll for the fleet soft restart could not be handed to 'serve-writer', the bus member "
        "the change acts for — its app is not built. The roll did not start; the reload broadcast reached the bus"
    )


async def test_a_soft_restart_whose_reload_and_roll_hand_off_both_fail_raises_the_reload_failure_naming_both(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The writer's own reload fails, then the roll's hand-off is refused: the raised error stays
    # the reload's failure with the fleet report, the roll's failure named on that report, and
    # its roll lists serve-b, which no roll restarted, as awaiting its respawn.
    _with_bus(monkeypatch)
    service, _admin, _bus = _service(
        FakeConfigStore(),
        admin=FakeReloadAdmin(raise_reload=RuntimeError("reload boom")),
        bus=_fleet(**{"serve-b": _MOVED}),
    )
    monkeypatch.setattr(instance, "_app", None)

    with caplog.at_level(logging.ERROR):
        async with bounded_run(60):
            with pytest.raises(FleetBroadcastError, match="reload boom") as exc:
                await service.reload_fleet(None, change="the fleet soft restart")

    assert {row.name for row in exc.value.report.results} == {"serve-writer", "serve-b"}
    assert exc.value.report.error == (
        "the recycle roll the change owes failed: RollNotHandedError: fleet recycle: the roll for the fleet soft "
        "restart could not be handed to 'serve-writer', the bus member the change acts for — its app is not built. "
        "The roll did not start; the reload broadcast reached the bus"
    )
    roll = exc.value.roll
    assert roll is not None
    assert roll.recycle is None
    assert roll.self_exit_armed is False
    assert [worker.model_dump() for worker in roll.awaiting_respawn] == [
        {"name": "serve-b", "keys": ["BACKEND_MANIFEST_KEY"]}
    ]
    assert "the recycle roll the change owes failed" in caplog.text
