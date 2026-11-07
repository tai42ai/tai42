"""The writer-exit scope: the exit a change holds and owes, carried from the change to the edge that answers it.

A change holds the worker's exit in the open scope from its start, one hold per scope; a roll owes
the writer's exit into it; the edge settles it once (:func:`~tai42_skeleton.app.graceful_exit.depart`),
releasing the hold when nothing is owed, or the scope abandons it on close — releasing the hold,
the debt ERROR-logged. A scope opened inside another owns the changes under it, unless an
enclosing one holds or owes the exit, which then takes the debt over; work spawned in a task under
a scope owes into that scope. A closed scope refuses a new hold or debt, and with no scope open a
debt is refused outright. A config door requires the hold the edge took: reached under an open scope
on a worker that owns its exit with no hold it raises; with no scope open, or on a forked job child,
it requires nothing.
"""

from __future__ import annotations

import asyncio
from typing import cast

import pytest

from tai42_skeleton.app.bus import WorkerBus, WorkerIdentity, WorkerKind
from tai42_skeleton.app.writer_exit import (
    WRITER_EXIT_REASON,
    WorkerLeavingError,
    open_writer_exit_scope,
    require_writer_exit_held,
    writer_exit_scope,
)

from .._fakes.bus import FakeBus


def _bus() -> FakeBus:
    return FakeBus(origin="serve-writer", remotes=[])


def _owe(bus: FakeBus, change: str = "the env change") -> None:
    scope = open_writer_exit_scope(change)
    scope.hold_exit(cast("WorkerBus", bus), change)
    scope.owe(cast("WorkerBus", bus))


async def test_a_settled_scope_departs_once_and_releases_every_hold_it_owns() -> None:
    bus = _bus()

    with writer_exit_scope() as exits:
        _owe(bus, "the first change")
        _owe(bus, "the second change")
        # One hold serves every change under the scope.
        assert bus.deferred_exits == ["the first change"]
        await exits.settle()

    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None


async def test_a_scope_owing_nothing_settles_to_no_exit() -> None:
    bus = _bus()

    with writer_exit_scope() as exits:
        await exits.settle()

    assert bus.exit_requests == []


async def test_a_scope_closed_unsettled_releases_its_holds_and_logs_the_debt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = _bus()

    async def edge() -> None:
        with writer_exit_scope():
            _owe(bus)
            raise ValueError("the edge failed")

    with caplog.at_level("ERROR"), pytest.raises(ValueError, match="the edge failed"):
        await edge()

    assert bus.exit_requests == []
    assert bus.exit_deferred is None
    assert any("its edge closed before the exit ran" in record.message for record in caplog.records)


async def test_a_settled_scope_refuses_a_later_debt() -> None:
    bus = _bus()

    with writer_exit_scope() as exits:
        await exits.settle()
        with pytest.raises(RuntimeError, match="no writer-exit scope is open"):
            _owe(bus)


def test_with_no_scope_open_a_debt_is_refused() -> None:
    with pytest.raises(RuntimeError, match="no writer-exit scope is open"):
        open_writer_exit_scope("the env change")


async def test_a_scope_opened_inside_another_owns_the_changes_under_it() -> None:
    bus = _bus()

    with writer_exit_scope() as outer:
        with writer_exit_scope() as inner:
            _owe(bus)
            await inner.settle()
        assert bus.exit_requests == [WRITER_EXIT_REASON]
        await outer.settle()

    # The outer scope owed nothing: one exit.
    assert bus.exit_requests == [WRITER_EXIT_REASON]


async def test_a_scope_settling_inside_one_that_already_owes_hands_it_its_debt() -> None:
    bus = _bus()

    with writer_exit_scope() as outer:
        _owe(bus, "the route's own change")
        with writer_exit_scope() as inner:
            _owe(bus, "a tool's change under the route")
            # Waiting here for the route's hold would never end: the route answers after this.
            await asyncio.wait_for(inner.settle(), timeout=1)
        assert bus.exit_requests == []
        assert bus.deferred_exits == ["the route's own change", "a tool's change under the route"]
        await outer.settle()

    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None


async def test_a_task_spawned_under_a_scope_owes_into_that_scope() -> None:
    bus = _bus()

    with writer_exit_scope() as exits:
        await asyncio.create_task(_owe_async(bus))
        await exits.settle()

    assert bus.exit_requests == [WRITER_EXIT_REASON]


async def _owe_async(bus: FakeBus) -> None:
    _owe(bus)


async def test_a_scope_holding_the_exit_and_owing_nothing_releases_it_on_settle() -> None:
    bus = _bus()

    with writer_exit_scope() as exits:
        exits.hold_exit(cast("WorkerBus", bus), "the env change")
        assert bus.exit_deferred == "the env change"
        await exits.settle()

    assert bus.exit_requests == []
    assert bus.exit_deferred is None


def _change_whose_edge_fails(bus: FakeBus) -> None:
    """A change that holds the exit in its scope, then fails before its edge answers."""
    with writer_exit_scope() as exits:
        exits.hold_exit(cast("WorkerBus", bus), "the env change")
        raise ValueError("the edge failed")


async def test_a_scope_holding_the_exit_closed_unsettled_releases_it_and_logs_no_debt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    bus = _bus()

    with caplog.at_level("ERROR"), pytest.raises(ValueError, match="the edge failed"):
        _change_whose_edge_fails(bus)

    assert bus.exit_deferred is None
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_with_no_scope_open_a_config_door_requires_no_hold() -> None:
    bus = _bus()

    require_writer_exit_held(cast("WorkerBus", bus), "the env change")

    assert bus.exit_deferred is None


async def test_a_config_door_reached_under_an_open_scope_with_no_hold_raises() -> None:
    bus = _bus()

    with writer_exit_scope(), pytest.raises(RuntimeError) as raised:
        require_writer_exit_held(cast("WorkerBus", bus), "the env change")

    assert str(raised.value) == (
        "writer exit: the env change reached a config door with no hold on the writer's exit — "
        "its operation or route declares holds_writer_exit"
    )
    assert bus.exit_deferred is None


async def test_a_config_door_reached_under_a_scope_holding_the_exit_takes_no_further_hold() -> None:
    bus = _bus()

    with writer_exit_scope() as exits:
        exits.hold_exit(cast("WorkerBus", bus), "the admitted change")
        require_writer_exit_held(cast("WorkerBus", bus), "the env change")
        assert bus.deferred_exits == ["the admitted change"]
        await exits.settle()

    assert bus.exit_deferred is None


async def test_a_config_door_on_a_process_that_owns_no_exit_requires_no_hold() -> None:
    bus = FakeBus(origin="backend-1/fork-7", remotes=["backend-1"])
    bus._identity = WorkerIdentity(name="backend-1/fork-7", kind=WorkerKind.backend, pid=7, generation=0, member=False)

    with writer_exit_scope():
        require_writer_exit_held(cast("WorkerBus", bus), "the env change")

    assert bus.exit_deferred is None


async def test_a_settled_scope_refuses_a_later_hold() -> None:
    bus = _bus()

    with writer_exit_scope() as exits:
        await exits.settle()
        with pytest.raises(RuntimeError, match="began after its edge already answered"):
            exits.hold_exit(cast("WorkerBus", bus), "the env change")


async def test_a_scope_owing_the_exit_inside_one_that_only_holds_it_hands_it_the_debt() -> None:
    bus = _bus()

    with writer_exit_scope() as outer:
        outer.hold_exit(cast("WorkerBus", bus), "the route's own change")
        with writer_exit_scope() as inner:
            _owe(bus, "a tool's change under the route")
            # Waiting here for the route's hold would never end: the route answers after this.
            await asyncio.wait_for(inner.settle(), timeout=1)
        assert bus.exit_requests == []
        await outer.settle()

    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None


async def test_a_change_on_a_worker_that_decided_to_leave_is_refused_before_it_holds() -> None:
    bus = _bus()
    assert bus.decide_departure("a fleet recycle op") is None

    with writer_exit_scope() as exits:
        with pytest.raises(WorkerLeavingError) as refused:
            exits.hold_exit(cast("WorkerBus", bus), "the env change")
        assert not exits.holds_exit

    assert str(refused.value) == (
        "leaving — this worker is leaving (a fleet recycle op) and begins no change; retry shortly"
    )
    assert refused.value.extra == {"leaving": "a fleet recycle op"}
    assert refused.value.status == 503
    assert bus.exit_deferred is None


async def test_a_scope_that_already_holds_the_exit_takes_no_second_hold_on_a_leaving_worker() -> None:
    bus = _bus()

    with writer_exit_scope() as exits:
        exits.hold_exit(cast("WorkerBus", bus), "the env change")
        assert bus.stop_began("SIGTERM received")
        # The change in flight goes on under the hold it took at its start.
        exits.hold_exit(cast("WorkerBus", bus), "a later change under the same edge")
        assert bus.deferred_exits == ["the env change"]
        await exits.settle()

    assert bus.exit_deferred is None
