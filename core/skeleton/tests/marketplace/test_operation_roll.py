"""A guarded marketplace operation runs the rolls its body deferred once the body is done, downgraded to shared."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest

from tai42_skeleton.app.bus import FleetResult, WorkerIdentity, WorkerKind
from tai42_skeleton.app.recycle import RecycleReport
from tai42_skeleton.config.recycle_policy import RecycleDecision
from tai42_skeleton.config.recycle_roll import RollFailedError, RollOutcome
from tai42_skeleton.config.service import DeferredRolls
from tai42_skeleton.config.service.deferred_roll import DeferredReload, active_deferral
from tai42_skeleton.marketplace import locks
from tai42_skeleton.marketplace.errors import LocalStateError, UnrunRollError
from tai42_skeleton.marketplace.operation_roll import guarded_operation

from .test_installer import FakeFleetHold, FakeFleetLock

_WRITER = WorkerIdentity(name="serve-writer", kind=WorkerKind.serve, pid=1, generation=1)


class _RollingService:
    """The pipeline's deferred-roll runner, recording each call against the shared event log.

    It raises ``fails`` when given, else returns ``outcome`` when given, else a converged empty roll.
    """

    def __init__(
        self, events: list[str], *, fails: BaseException | None = None, outcome: RollOutcome | None = None
    ) -> None:
        self._events = events
        self._fails = fails
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    async def run_deferred_rolls(
        self, deferral: DeferredRolls, *, writer_exits: bool, change: str, holds: Sequence[str]
    ) -> RollOutcome:
        self._events.append("roll")
        merged = deferral.merged()
        assert merged is not None
        self.calls.append(
            {"pinned": dict(merged.pinned), "writer_exits": writer_exits, "change": change, "holds": tuple(holds)}
        )
        if self._fails is not None:
            raise self._fails
        if self._outcome is not None:
            return self._outcome
        decision = RecycleDecision(
            writer=_WRITER,
            reported={},
            unconfirmed={},
            clean=frozenset(),
            writer_reason=None,
            withheld={},
            addressed=None,
            report_unreachable=False,
        )
        return RollOutcome(
            decision=decision, recycle=RecycleReport(rows=[]), awaiting_respawn=[], self_exit_armed=False
        )


def _defer(name: str, generation: int) -> None:
    """A config change inside the operation deferring its recycle decision over ``name``."""
    deferral = active_deferral()
    assert deferral is not None
    deferral.reloads.append(
        DeferredReload(
            writer=_WRITER, pinned={name: (WorkerKind.serve, generation)}, report=FleetResult(op="reload_config")
        )
    )


async def test_the_body_s_deferred_rolls_run_once_after_it_with_the_hold_downgraded() -> None:
    events: list[str] = []
    svc = _RollingService(events)

    async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op") as operation:
        _defer("serve-b", 1)
        _defer("serve-c", 2)
        events.append("body:done")

    assert events == ["lock:acquire", "body:done", "lock:downgrade", "roll", "lock:release"]
    assert svc.calls == [
        {
            "pinned": {"serve-b": (WorkerKind.serve, 1), "serve-c": (WorkerKind.serve, 2)},
            "writer_exits": True,
            "change": "the op",
            # A roll handed off keeps the fleet key held shared until it ended.
            "holds": (locks.MARKETPLACE_ROLL_HOLD,),
        }
    ]
    assert operation.roll is not None
    assert locks.operation_lock.locked() is False


async def test_a_body_that_deferred_nothing_never_downgrades_or_rolls() -> None:
    events: list[str] = []
    svc = _RollingService(events)

    async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op") as operation:
        pass

    assert events == ["lock:acquire", "lock:release"]
    assert operation.roll is None
    assert operation.settled(None) is None


async def _failing_operation(
    events: list[str], svc: _RollingService, *, deferred: int, error: Exception | None = None
) -> None:
    """An operation whose body deferred ``deferred`` rolls (its forward change, its unwind's revert), then failed.

    The body raises ``error``, a plain ``RuntimeError("record failed")`` when none is given.
    """
    async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op"):
        for _ in range(deferred):
            _defer("serve-b", 1)
        raise RuntimeError("record failed") if error is None else error


async def test_a_failed_body_s_landed_rolls_still_run_with_no_writer_exit_and_its_error_is_raised() -> None:
    events: list[str] = []
    svc = _RollingService(events)

    with pytest.raises(RuntimeError, match="record failed"):
        await _failing_operation(events, svc, deferred=2)

    assert events == ["lock:acquire", "lock:downgrade", "roll", "lock:release"]
    assert [call["writer_exits"] for call in svc.calls] == [False]


async def test_a_fleet_broadcast_failure_of_the_body_carries_its_landed_roll_with_the_writer_s_exit_owed() -> None:
    # A plugin that cannot load fails the body's reload as a FleetBroadcastError, whose error
    # reply renders the roll and arms the writer's exit: the roll runs with the writer's exit
    # owed and rides the error, so its reply states the roll handed to the member carrying it.
    from tai42_skeleton.app.bus import OpOutcome, WorkerResult
    from tai42_skeleton.app.error_envelope import fleet_broadcast_error_handler
    from tai42_skeleton.marketplace.compat import CorePluginBootError
    from tai42_skeleton.operations._broadcast import FleetBroadcastError

    events: list[str] = []
    decision = RecycleDecision(
        writer=_WRITER,
        reported={},
        unconfirmed={},
        clean=frozenset(),
        writer_reason=None,
        withheld={},
        addressed=None,
        report_unreachable=False,
    )
    outcome = RollOutcome(
        decision=decision, recycle=None, awaiting_respawn=[], self_exit_armed=False, deferred_to="backend-writer"
    )
    svc = _RollingService(events, outcome=outcome)
    report = FleetResult(
        op="reload_config",
        results=[WorkerResult(name="serve-writer", outcome=OpOutcome.failed, error="CorePluginBootError: x")],
    )
    cause = CorePluginBootError("plugin 'acme-tools' cannot load: its module failed to import")
    error = FleetBroadcastError("reload_config", report, cause)
    error.__cause__ = cause

    with pytest.raises(FleetBroadcastError) as raised:
        await _failing_operation(events, svc, deferred=1, error=error)

    assert [call["writer_exits"] for call in svc.calls] == [True]
    assert raised.value.roll is outcome
    response = await fleet_broadcast_error_handler(cast("Any", None), raised.value)
    body = json.loads(bytes(response.body))
    assert body["error"] == "plugin 'acme-tools' cannot load: its module failed to import"
    assert (body["recycle"], body["awaiting_respawn"], body["roll_deferred"], body["roll_deferred_to"]) == (
        None,
        [],
        True,
        "backend-writer",
    )
    assert events[-1] == "lock:release"


async def test_a_failed_roll_after_a_marketplace_failure_is_noted_on_the_body_s_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    svc = _RollingService(events, fails=ConnectionError("bus gone"))

    with pytest.raises(LocalStateError, match="record failed") as raised:
        await _failing_operation(events, svc, deferred=1, error=LocalStateError("record failed"))

    assert raised.value.__notes__ == [
        "config changes landed, but the fleet recycle roll they owe did not complete: ConnectionError"
    ]
    # The roll's own error is logged whole, never put in the note.
    assert "the fleet recycle roll the op owes did not complete" in caplog.text
    assert "bus gone" in caplog.text
    assert events[-1] == "lock:release"


async def test_a_failed_roll_after_any_other_failure_raises_the_unrun_roll_naming_the_failure() -> None:
    events: list[str] = []
    svc = _RollingService(events, fails=ConnectionError("bus gone"))

    with pytest.raises(UnrunRollError) as raised:
        await _failing_operation(events, svc, deferred=1)

    assert (
        str(raised.value)
        == "config changes landed, but the fleet recycle roll they owe did not complete: ConnectionError"
    )
    assert raised.value.__notes__ == ["the operation failed before its roll ran: RuntimeError"]
    # The chain keeps both errors: the roll's, raised while the body's was handled.
    assert isinstance(raised.value.__cause__, ConnectionError)
    assert isinstance(raised.value.__cause__.__context__, RuntimeError)
    assert events[-1] == "lock:release"


async def test_a_failed_roll_names_the_error_that_stopped_it_in_the_unrun_roll_line() -> None:
    # The pipeline raises a failed roll as RollFailedError from what stopped it: the line names
    # that error's type, and the chain keeps it as the cause.
    events: list[str] = []
    decision = RecycleDecision(
        writer=_WRITER,
        reported={},
        unconfirmed={},
        clean=frozenset(),
        writer_reason=None,
        withheld={},
        addressed=None,
        report_unreachable=False,
    )
    outcome = RollOutcome(decision=decision, recycle=None, awaiting_respawn=[], self_exit_armed=False)
    svc = _RollingService(events, fails=RollFailedError(ConnectionError("bus gone"), outcome))

    with pytest.raises(UnrunRollError) as raised:
        async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op"):
            _defer("serve-b", 1)

    assert (
        str(raised.value)
        == "config changes landed, but the fleet recycle roll they owe did not complete: ConnectionError"
    )
    assert isinstance(raised.value.__cause__, ConnectionError)


class _DroppingHold(FakeFleetHold):
    """A fleet hold whose session dropped once the body's changes were made: the downgrade raises."""

    async def downgrade(self) -> None:
        self._events.append("lock:downgrade")
        raise ConnectionError("server closed the connection unexpectedly")


@asynccontextmanager
async def _dropping_lock(events: list[str]) -> AsyncIterator[FakeFleetHold]:
    events.append("lock:acquire")
    try:
        yield _DroppingHold(events)
    finally:
        events.append("lock:release")


async def test_a_session_drop_after_the_body_s_changes_notes_that_they_landed_and_the_roll_did_not_run(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    svc = _RollingService(events)

    with pytest.raises(UnrunRollError) as raised:
        async with guarded_operation(lambda: _dropping_lock(events), lambda: cast("Any", svc), change="the op"):
            _defer("serve-b", 1)

    assert svc.calls == []
    assert (
        str(raised.value)
        == "config changes landed, but the fleet recycle roll they owe did not complete: ConnectionError"
    )
    assert isinstance(raised.value.__cause__, ConnectionError)
    assert "the fleet recycle roll the op owes did not complete" in caplog.text
    assert "server closed the connection unexpectedly" in caplog.text
    assert events == ["lock:acquire", "lock:downgrade", "lock:release"]
    assert locks.operation_lock.locked() is False


async def test_a_roll_that_raises_after_the_body_s_changes_raises_that_they_landed() -> None:
    events: list[str] = []
    svc = _RollingService(events, fails=RuntimeError("census contract violated"))

    with pytest.raises(UnrunRollError) as raised:
        async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op"):
            _defer("serve-b", 1)

    assert (
        str(raised.value) == "config changes landed, but the fleet recycle roll they owe did not complete: RuntimeError"
    )
    assert isinstance(raised.value.__cause__, RuntimeError)


async def test_a_body_cancelled_after_its_changes_landed_propagates_with_the_unrun_roll_noted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    svc = _RollingService(events)

    async def cancelled_operation() -> None:
        async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op"):
            _defer("serve-b", 1)
            raise asyncio.CancelledError("job cancelled")

    with pytest.raises(asyncio.CancelledError) as raised:
        await cancelled_operation()

    # A cancelled operation starts no minutes-long roll: it notes the roll and lets the cancel through.
    assert svc.calls == []
    assert events == ["lock:acquire", "lock:release"]
    assert raised.value.__notes__ == [
        "config changes landed, but the fleet recycle roll they owe did not complete: the operation was cancelled"
    ]
    assert "the fleet recycle roll the op owes did not complete" in caplog.text


async def test_a_body_cancelled_before_any_change_landed_notes_nothing() -> None:
    events: list[str] = []
    svc = _RollingService(events)

    with pytest.raises(asyncio.CancelledError) as raised:
        async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op"):
            raise asyncio.CancelledError

    assert getattr(raised.value, "__notes__", []) == []
    assert svc.calls == []


async def test_a_roll_cancelled_mid_way_propagates_with_the_unrun_roll_noted() -> None:
    events: list[str] = []
    svc = _RollingService(events, fails=asyncio.CancelledError("shutdown"))

    with pytest.raises(asyncio.CancelledError) as raised:
        async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op"):
            _defer("serve-b", 1)

    assert raised.value.__notes__ == [
        "config changes landed, but the fleet recycle roll they owe did not complete: the operation was cancelled"
    ]
    assert events[-1] == "lock:release"


async def test_a_failed_body_s_roll_cancelled_mid_way_propagates_the_cancel_with_the_unrun_roll_noted() -> None:
    events: list[str] = []
    svc = _RollingService(events, fails=asyncio.CancelledError("shutdown"))

    with pytest.raises(asyncio.CancelledError) as raised:
        await _failing_operation(events, svc, deferred=1)

    assert raised.value.__notes__ == [
        "config changes landed, but the fleet recycle roll they owe did not complete: the operation was cancelled"
    ]
    assert isinstance(raised.value.__context__, RuntimeError)


async def test_a_roll_with_no_result_to_carry_it_raises() -> None:
    events: list[str] = []
    svc = _RollingService(events)

    async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op") as operation:
        _defer("serve-b", 1)

    with pytest.raises(RuntimeError, match="reported no config result"):
        operation.settled(None)
