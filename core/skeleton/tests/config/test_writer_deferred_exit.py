"""The writer's deferred exit is a fact its bus holds while the change that defers it runs.

:func:`~tai42_skeleton.config.recycle_roll.decide_and_roll` is where every door's roll defers the
writer's own restart to its post-reply exit. The writer's bus holds the exit in the open
writer-exit scope, from the change's start when its door took the hold, and the roll reuses that
hold, taking it itself when the door took none; while it is held the bus answers a foreign
``Recycle`` op with ``exit_deferred`` instead of latching an exit mid-reply. The roll owes the exit
into the scope when it arms it (the scope's settle departs and releases the hold); when it does
not (the roll stopped, or raised) it owes nothing and the scope releases the hold once its edge
answered. A roll that would defer the exit with no scope open raises before anything rolls.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

# ``bus_settings`` is imported for its registration side effect: TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerBus, WorkerKind, WorkerResult
from tai42_skeleton.app.recycle import TIMED_OUT, RecycleError, RecycleReport, RecycleRow, RecycleStop
from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON, hold_writer_exit, writer_exit_scope
from tai42_skeleton.config.recycle_roll import RollFailedError, decide_and_roll

from .._fakes.bus import FakeBus

_KEY = "TAI_BUS_HEARTBEAT_TTL"
_CHANGE = "this config change"


def _report(*, writer_keys: list[str]) -> FleetResult:
    return FleetResult(
        op="reload_config",
        results=[
            WorkerResult(name="serve-writer", outcome=OpOutcome.applied, payload={"awaiting_respawn": writer_keys}),
            WorkerResult(name="serve-b", outcome=OpOutcome.applied, payload={"awaiting_respawn": [_KEY]}),
        ],
    )


def _orchestrate(bus: FakeBus, seen: list[str | None], *, report: RecycleReport | None = None, raises: Any = None):
    async def orchestrate(_bus: Any, **_kwargs: Any) -> RecycleReport:
        seen.append(bus.exit_deferred)
        if raises is not None:
            raise raises
        return report if report is not None else RecycleReport()

    return orchestrate


async def _roll(bus: FakeBus, orchestrate: Any, *, writer_keys: list[str], writer_exits: bool = True) -> Any:
    with writer_exit_scope():
        return await _roll_unscoped(bus, orchestrate, writer_keys=writer_keys, writer_exits=writer_exits)


async def _roll_unscoped(bus: FakeBus, orchestrate: Any, *, writer_keys: list[str], writer_exits: bool = True) -> Any:
    return await decide_and_roll(
        cast("WorkerBus", bus),
        _report(writer_keys=writer_keys),
        bus.member_identity,
        {"serve-b": (WorkerKind.serve, 1)},
        change=_CHANGE,
        orchestrate=orchestrate,
        writer_exits=writer_exits,
    )


async def test_the_writer_holds_its_deferred_exit_while_the_roll_runs_and_owes_it_once_armed() -> None:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    seen: list[str | None] = []

    with writer_exit_scope() as exits:
        outcome = await _roll_unscoped(bus, _orchestrate(bus, seen), writer_keys=[_KEY])
        # Armed: the hold stays held until the edge that answers the change settles the exit.
        assert outcome.self_exit_armed is True
        assert bus.exit_deferred == _CHANGE
        await exits.settle()

    assert seen == [_CHANGE]
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None


async def test_an_armed_roll_whose_edge_never_settles_releases_the_hold_and_logs_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])

    with caplog.at_level("ERROR"), writer_exit_scope():
        await _roll_unscoped(bus, _orchestrate(bus, []), writer_keys=[_KEY])

    assert bus.exit_requests == []
    assert bus.exit_deferred is None
    assert any("owes this writer's own restart" in record.message for record in caplog.records)


async def test_a_roll_owing_the_writers_exit_with_no_scope_open_raises_before_it_rolls() -> None:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    seen: list[str | None] = []

    with pytest.raises(RuntimeError, match="no writer-exit scope is open"):
        await _roll_unscoped(bus, _orchestrate(bus, seen), writer_keys=[_KEY])

    assert seen == []
    assert bus.exit_deferred is None


async def test_a_roll_owing_nothing_needs_no_scope() -> None:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])

    outcome = await _roll_unscoped(bus, _orchestrate(bus, []), writer_keys=[])

    assert outcome.self_exit_armed is False


async def test_a_roll_that_stopped_owes_nothing_and_its_scope_releases_the_hold() -> None:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    seen: list[str | None] = []
    stopped = RecycleReport(
        rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=TIMED_OUT)],
        stopped=RecycleStop(kind="serve", name="serve-b", detail="old life still present"),
    )

    with writer_exit_scope() as exits:
        outcome = await _roll_unscoped(
            bus, _orchestrate(bus, seen, raises=RecycleError("stopped", stopped)), writer_keys=[_KEY]
        )
        # The change still runs until its edge answers: the hold stays until then.
        assert bus.exit_deferred == _CHANGE
        await exits.settle()

    assert seen == [_CHANGE]
    assert outcome.self_exit_armed is False
    assert bus.exit_requests == []
    assert bus.exit_deferred is None


async def test_a_roll_that_raises_with_no_recorded_stop_leaves_the_hold_to_its_scope_to_release() -> None:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    seen: list[str | None] = []

    with pytest.raises(RollFailedError) as raised:
        await _roll(bus, _orchestrate(bus, seen, raises=RecycleError("no row", RecycleReport())), writer_keys=[_KEY])

    assert isinstance(raised.value.failure, RecycleError)
    assert raised.value.outcome.self_exit_armed is False
    assert seen == [_CHANGE]
    assert bus.exit_deferred is None


async def test_a_writer_whose_exit_does_not_follow_defers_nothing() -> None:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    seen: list[str | None] = []

    await _roll(bus, _orchestrate(bus, seen), writer_keys=[_KEY], writer_exits=False)

    assert seen == [None]
    assert bus.deferred_exits == []


async def test_a_clean_writer_defers_nothing() -> None:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    seen: list[str | None] = []

    await _roll(bus, _orchestrate(bus, seen), writer_keys=[])

    assert seen == [None]
    assert bus.deferred_exits == []


async def test_the_roll_reuses_the_hold_its_change_took_at_its_start() -> None:
    bus = FakeBus(origin="serve-writer", remotes=["serve-b"])
    seen: list[str | None] = []

    with writer_exit_scope() as exits:
        hold_writer_exit(cast("WorkerBus", bus), "this config change")
        outcome = await _roll_unscoped(bus, _orchestrate(bus, seen), writer_keys=[_KEY])
        # One hold, the change's own: the roll took no second one.
        assert bus.deferred_exits == ["this config change"]
        await exits.settle()

    assert outcome.self_exit_armed is True
    assert seen == ["this config change"]
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None
