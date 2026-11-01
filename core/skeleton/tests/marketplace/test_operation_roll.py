"""A guarded marketplace operation runs the rolls its body deferred once the body is done, downgraded to shared."""

from __future__ import annotations

from typing import Any, cast

import pytest

from tai42_skeleton.app.bus import WorkerIdentity, WorkerKind
from tai42_skeleton.app.recycle import RecycleReport
from tai42_skeleton.config.service import DeferredRolls, RollOutcome
from tai42_skeleton.config.service.deferred_roll import PendingRoll, active_deferral
from tai42_skeleton.marketplace import locks
from tai42_skeleton.marketplace.operation_roll import guarded_operation

from .test_installer import FakeFleetLock

_WRITER = WorkerIdentity(name="serve-writer", kind=WorkerKind.serve, pid=1, generation=1)


class _RollingService:
    """The pipeline's deferred-roll runner, recording each call against the shared event log."""

    def __init__(self, events: list[str], *, fails: Exception | None = None) -> None:
        self._events = events
        self._fails = fails
        self.calls: list[dict[str, Any]] = []

    async def run_deferred_rolls(self, deferral: DeferredRolls, *, writer_exits: bool, change: str) -> RollOutcome:
        self._events.append("roll")
        merged = deferral.merged()
        assert merged is not None
        self.calls.append({"pinned": dict(merged.pinned), "writer_exits": writer_exits, "change": change})
        if self._fails is not None:
            raise self._fails
        return RollOutcome(recycle=RecycleReport(rows=[]), self_identity=_WRITER, awaiting_respawn=[])


def _defer(name: str, generation: int) -> None:
    """A config change inside the operation deferring its roll over ``name``."""
    deferral = active_deferral()
    assert deferral is not None
    deferral.rolls.append(PendingRoll(identity=_WRITER, pinned={name: (WorkerKind.serve, generation)}))


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
        }
    ]
    assert operation.roll is not None
    assert locks.operation_lock.locked() is False


async def test_a_batch_that_arms_no_writer_exit_rolls_with_none() -> None:
    events: list[str] = []
    svc = _RollingService(events)

    async with guarded_operation(
        FakeFleetLock(events), lambda: cast("Any", svc), change="the batch", writer_exits=False
    ):
        _defer("serve-b", 1)

    assert [call["writer_exits"] for call in svc.calls] == [False]


async def test_a_body_that_deferred_nothing_never_downgrades_or_rolls() -> None:
    events: list[str] = []
    svc = _RollingService(events)

    async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op") as operation:
        pass

    assert events == ["lock:acquire", "lock:release"]
    assert operation.roll is None
    assert operation.settled(None) is None


async def _failing_operation(events: list[str], svc: _RollingService, *, deferred: int) -> None:
    """An operation whose body deferred ``deferred`` rolls (its forward change, its unwind's revert), then failed."""
    async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op"):
        for _ in range(deferred):
            _defer("serve-b", 1)
        raise RuntimeError("record failed")


async def test_a_failed_body_s_landed_rolls_still_run_with_no_writer_exit_and_its_error_is_raised() -> None:
    events: list[str] = []
    svc = _RollingService(events)

    with pytest.raises(RuntimeError, match="record failed"):
        await _failing_operation(events, svc, deferred=2)

    assert events == ["lock:acquire", "lock:downgrade", "roll", "lock:release"]
    assert [call["writer_exits"] for call in svc.calls] == [False]


async def test_a_failed_roll_after_a_failed_body_is_noted_on_the_body_s_error(caplog: pytest.LogCaptureFixture) -> None:
    events: list[str] = []
    svc = _RollingService(events, fails=ConnectionError("bus gone"))

    with pytest.raises(RuntimeError, match="record failed") as raised:
        await _failing_operation(events, svc, deferred=1)

    assert raised.value.__notes__ == ["the fleet recycle roll after the failure also failed: ConnectionError: bus gone"]
    assert "the fleet recycle roll after the op failed also failed" in caplog.text
    assert events[-1] == "lock:release"


async def test_a_roll_with_no_result_to_carry_it_raises() -> None:
    events: list[str] = []
    svc = _RollingService(events)

    async with guarded_operation(FakeFleetLock(events), lambda: cast("Any", svc), change="the op") as operation:
        _defer("serve-b", 1)

    with pytest.raises(RuntimeError, match="reported no config result"):
        operation.settled(None)
