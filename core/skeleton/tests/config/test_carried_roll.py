"""The member a bounded change hands its roll to carries it out as its own task, then departs last when owed.

The ``carry_roll`` op (over the bus, or given to the member's own app) is answered at once: a
member already leaving carries nothing; otherwise it holds its own exit for the change from the
answer on, so a ``Recycle`` op from another worker's roll is deferred to it, and rolls after the
answer as a tracked task. The member takes the roll holds the op names before it answers and keeps
them until the roll ended; a hold it cannot take leaves the op unapplied with nothing carried. A
converged roll whose writer needs its own restart departs the member once the roll ended; any
other roll releases the hold and departs nothing. Shutdown cancels the task, logged at ERROR.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tai42_contract.app import tai42_app

# ``bus_settings`` is imported for its registration side effect: TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
import tai42_skeleton.app.lifecycle.carried_roll as carried_roll_module
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerIdentity, WorkerKind, WorkerResult
from tai42_skeleton.app.bus.ops import CarryRoll
from tai42_skeleton.app.graceful_exit import leave_when_settled
from tai42_skeleton.app.lifecycle import TaiMCPLifecycleMixin
from tai42_skeleton.app.recycle import RECYCLED, TIMED_OUT, RecycleError, RecycleReport, RecycleRow, RecycleStop
from tai42_skeleton.app.writer_exit import WRITER_EXIT_REASON
from tai42_skeleton.config import roll_handoff, roll_holds

from .._fakes.bus import FakeBus
from .fake_pipeline import _reset_settings_after, _with_bus  # noqa: F401  — autouse fixture, active on import

tai42_app.bind(instance.app)

_KEY = "TAI_BUS_HEARTBEAT_TTL"
_CHANGE = "this config change"
_MEMBER = "backend-writer"


class _Lifecycle(TaiMCPLifecycleMixin):
    """Concrete-enough lifecycle for the fleet-op handlers: no server, no config manager."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.preset_manager = cast("Any", type("_Presets", (), {"reconcile_bases": AsyncMock()})())

    def _mcp_tools(self, config: Any, tools: Any, preset_names: frozenset[str]) -> None:  # pragma: no cover - unused
        return None


@pytest.fixture
def member(monkeypatch: pytest.MonkeyPatch) -> tuple[_Lifecycle, FakeBus]:
    _with_bus(monkeypatch)
    lifecycle = _Lifecycle()
    bus = FakeBus(origin=_MEMBER, remotes=["serve-b"])
    bus._identity = WorkerIdentity(name=_MEMBER, kind=WorkerKind.backend, pid=1, generation=3)
    lifecycle._bus = cast("Any", bus)
    return lifecycle, bus


def _op(*, writer_keys: list[str], writer_exits: bool = True, holds: tuple[str, ...] = ()) -> CarryRoll:
    return CarryRoll(
        op="carry_roll",
        change=_CHANGE,
        report=FleetResult(
            op="reload_config",
            results=[
                WorkerResult(name=_MEMBER, outcome=OpOutcome.applied, payload={"awaiting_respawn": writer_keys}),
                WorkerResult(name="serve-b", outcome=OpOutcome.applied, payload={"awaiting_respawn": [_KEY]}),
            ],
        ),
        pinned={"serve-b": (WorkerKind.serve, 1)},
        writer=WorkerIdentity(name=_MEMBER, kind=WorkerKind.backend, pid=1, generation=3),
        writer_exits=writer_exits,
        holds=holds,
    )


def _rolls(
    monkeypatch: pytest.MonkeyPatch, seen: list[str | None], bus: FakeBus, *, report: RecycleReport | None = None
) -> None:
    async def orchestrate(_bus: Any, **_kwargs: Any) -> RecycleReport:
        seen.append(bus.exit_deferred)
        if report is not None and report.stopped is not None:
            raise RecycleError("stopped", report)
        return report if report is not None else _recycled()

    monkeypatch.setattr(carried_roll_module, "orchestrate_recycle", orchestrate)


def _recycled() -> RecycleReport:
    return RecycleReport(rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=RECYCLED)])


async def _carried(lifecycle: _Lifecycle) -> None:
    await asyncio.wait_for(asyncio.gather(*lifecycle._carried_roll_tasks), timeout=5)


async def test_the_member_answers_before_rolling_and_departs_last_after_a_converged_roll(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)

    answer = await lifecycle._dispatch_bus_op(_op(writer_keys=[_KEY]))

    assert answer == {"carrying": _CHANGE}
    # Answered, not rolled yet: the member's exit is held for the change from the answer on.
    assert seen == []
    assert bus.exit_deferred == _CHANGE
    await _carried(lifecycle)
    assert seen == [_CHANGE]
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert bus.exit_deferred is None
    assert lifecycle._carried_roll_tasks == set()


@pytest.mark.parametrize(("writer_keys", "writer_exits"), [([], True), ([_KEY], False)], ids=["clean", "no-exit"])
async def test_a_converged_roll_departs_nothing_when_the_writer_owes_no_exit(
    member: tuple[_Lifecycle, FakeBus],
    monkeypatch: pytest.MonkeyPatch,
    writer_keys: list[str],
    writer_exits: bool,
) -> None:
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)

    await lifecycle.accept_carried_roll(_op(writer_keys=writer_keys, writer_exits=writer_exits))
    await _carried(lifecycle)

    assert seen == [_CHANGE]
    assert bus.exit_requests == []
    assert bus.exit_deferred is None


async def test_a_stopped_roll_is_logged_and_departs_nothing(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lifecycle, bus = member
    stopped = RecycleReport(
        rows=[RecycleRow(name="serve-b", kind="serve", generation_before=1, status=TIMED_OUT)],
        stopped=RecycleStop(kind="serve", name="serve-b", detail="old life still present"),
    )
    _rolls(monkeypatch, [], bus, report=stopped)

    with caplog.at_level(logging.ERROR):
        await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY]))
        await _carried(lifecycle)

    messages = [record.getMessage() for record in caplog.records]
    assert any("did not converge" in message and _CHANGE in message for message in messages), messages
    assert bus.exit_requests == []
    assert bus.exit_deferred is None


async def test_a_recycle_op_reaching_the_member_mid_roll_is_deferred_to_the_roll(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, bus = member
    answers: list[dict[str, Any]] = []

    async def orchestrate(_bus: Any, **_kwargs: Any) -> RecycleReport:
        answers.append(await lifecycle._apply_recycle())
        return _recycled()

    monkeypatch.setattr(carried_roll_module, "orchestrate_recycle", orchestrate)

    await lifecycle.accept_carried_roll(_op(writer_keys=[]))
    await _carried(lifecycle)
    deferred = lifecycle._deferred_recycle_task
    assert deferred is not None
    await asyncio.wait_for(asyncio.shield(deferred), timeout=5)

    assert answers == [{"recycling": "backend", "exit_deferred": _CHANGE}]
    assert bus.post_reply_armed == []
    # Once the roll ended the deferred recycle op is carried out: the member leaves for it.
    assert bus.exit_requests == ["a fleet recycle op"]


async def test_shutdown_cancels_a_carried_roll_with_an_error_line(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lifecycle, bus = member
    rolling = asyncio.Event()

    async def orchestrate(_bus: Any, **_kwargs: Any) -> RecycleReport:
        rolling.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(carried_roll_module, "orchestrate_recycle", orchestrate)
    await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY]))
    await asyncio.wait_for(rolling.wait(), timeout=5)

    with caplog.at_level(logging.ERROR):
        await lifecycle._cancel_bus_subscription()

    assert any("cancelled mid-roll" in record.getMessage() for record in caplog.records)
    assert lifecycle._carried_roll_tasks == set()
    assert bus.exit_requests == []
    assert bus.exit_deferred is None


async def test_a_carried_roll_cancelled_before_it_started_releases_its_hold_and_logs_it(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lifecycle, bus = member
    rolled: list[str | None] = []
    _rolls(monkeypatch, rolled, bus)

    await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY]))
    # The answer was given; the carried task has not resumed to start the roll yet.
    with caplog.at_level(logging.ERROR):
        await lifecycle._cancel_bus_subscription()

    assert rolled == []
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert any(_CHANGE in message and "before it started" in message for message in messages)
    assert bus.exit_requests == []
    assert bus.exit_deferred is None


async def test_a_carried_roll_cancelled_before_its_first_step_fails_the_answer_waiting_on_its_holds(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, bus = member
    _rolls(monkeypatch, [], bus)

    answer = asyncio.create_task(lifecycle.accept_carried_roll(_op(writer_keys=[_KEY])))
    # The answer spawned the carried task and waits on its holds; the task has not run a step yet.
    await asyncio.sleep(0)
    assert len(lifecycle._carried_roll_tasks) == 1
    await lifecycle._cancel_bus_subscription()

    with pytest.raises(RuntimeError, match="stopped before it took the holds"):
        await asyncio.wait_for(answer, timeout=5)
    assert lifecycle._carried_roll_tasks == set()
    assert bus.exit_requests == []
    assert bus.exit_deferred is None


def _register_holds(monkeypatch: pytest.MonkeyPatch, events: list[str], *, refuse: str | None = None) -> None:
    """Register the holds ``first`` and ``second``, recording entries and exits; ``refuse`` names one that fails."""

    def recording(name: str) -> Any:
        @asynccontextmanager
        async def hold() -> AsyncIterator[None]:
            if name == refuse:
                raise ConnectionError(f"{name} could not be taken")
            events.append(f"enter {name}")
            try:
                yield
            finally:
                events.append(f"exit {name}")

        return hold

    monkeypatch.setattr(roll_holds, "_HOLDS", {"first": recording("first"), "second": recording("second")})


async def test_the_member_enters_the_op_s_holds_before_answering_and_exits_them_once_the_roll_ended(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, bus = member
    events: list[str] = []
    _register_holds(monkeypatch, events)

    async def orchestrate(_bus: Any, **_kwargs: Any) -> RecycleReport:
        events.append("roll")
        return _recycled()

    monkeypatch.setattr(carried_roll_module, "orchestrate_recycle", orchestrate)

    answer = await lifecycle._dispatch_bus_op(_op(writer_keys=[_KEY], holds=("first", "second")))

    assert answer == {"carrying": _CHANGE}
    # Answered with every hold already taken, and the roll not run yet.
    assert events == ["enter first", "enter second"]
    await _carried(lifecycle)
    assert events == ["enter first", "enter second", "roll", "exit second", "exit first"]
    assert bus.exit_requests == [WRITER_EXIT_REASON]


@pytest.mark.parametrize("refuse", ["second", "unregistered"])
async def test_a_hold_the_member_cannot_take_leaves_the_op_unapplied_and_carries_nothing(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, refuse: str
) -> None:
    lifecycle, bus = member
    events: list[str] = []
    seen: list[str | None] = []
    _register_holds(monkeypatch, events, refuse=refuse)
    _rolls(monkeypatch, seen, bus)

    with pytest.raises((ConnectionError, LookupError), match=refuse):
        await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY], holds=("first", refuse)))

    # Nothing rolled, every hold taken was given back, and the member's exit is not held.
    assert seen == []
    assert events == (["enter first", "exit first"] if refuse == "second" else [])
    assert lifecycle._carried_roll_tasks == set()
    assert bus.exit_deferred is None
    assert bus.exit_requests == []


async def test_a_member_already_leaving_carries_nothing(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)
    bus.request_graceful_exit("a fleet recycle op")

    answer = await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY]))

    assert answer == {"already_leaving": "a fleet recycle op"}
    assert lifecycle._carried_roll_tasks == set()
    assert bus.exit_deferred is None
    assert seen == []


async def test_a_carry_reaching_a_member_that_decided_to_leave_for_its_writer_answers_already_leaving(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)
    answers: list[dict[str, str]] = []

    async def mark_recycling() -> None:
        # A carry reaching the member while it writes its departing state, after it decided to leave.
        answers.append(await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY])))

    monkeypatch.setattr(bus, "mark_recycling", mark_recycling)

    assert await leave_when_settled(cast("Any", bus), WRITER_EXIT_REASON) is True

    assert answers == [{"already_leaving": WRITER_EXIT_REASON}]
    assert lifecycle._carried_roll_tasks == set()
    assert bus.exit_deferred is None
    assert bus.exit_requests == [WRITER_EXIT_REASON]
    assert seen == []


async def test_a_carry_reaching_a_member_that_decided_to_leave_for_a_recycle_op_answers_already_leaving(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)
    answers: list[dict[str, str]] = []

    async def mark_recycling() -> None:
        answers.append(await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY])))

    monkeypatch.setattr(bus, "mark_recycling", mark_recycling)

    assert await lifecycle._apply_recycle() == {"recycling": "backend"}

    assert answers == [{"already_leaving": "a fleet recycle op"}]
    assert lifecycle._carried_roll_tasks == set()
    assert bus.exit_deferred is None
    assert seen == []


async def test_a_change_in_the_member_off_its_serving_loop_hands_the_roll_onto_that_loop(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, _bus = member
    loops: list[asyncio.AbstractEventLoop] = []

    async def orchestrate(_bus: Any, **_kwargs: Any) -> RecycleReport:
        loops.append(asyncio.get_running_loop())
        return _recycled()

    monkeypatch.setattr(carried_roll_module, "orchestrate_recycle", orchestrate)
    serving = asyncio.get_running_loop()
    lifecycle.bind_serving_loop()
    answers: list[dict[str, str]] = []

    def off_loop() -> None:
        # A job on a backend worker's private task loop, in another thread.
        answers.append(asyncio.run(lifecycle.carry_roll(_op(writer_keys=[]))))

    thread = threading.Thread(target=off_loop)
    thread.start()
    await asyncio.to_thread(thread.join, 5)
    await _carried(lifecycle)

    assert answers == [{"carrying": _CHANGE}]
    assert loops == [serving]


async def test_a_carry_op_failed_by_a_post_apply_handler_after_the_roll_started_is_never_read_as_unstarted(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    # A raising ``on_fleet_op_applied`` handler fails the op once the member took the roll: the
    # roll runs, so the sender reading the ``failed`` row says its start is unknown.
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)

    def a_handler_that_fails(_applied: Any) -> None:
        raise RuntimeError("a post-apply handler failed")

    lifecycle._on_fleet_op_applied(a_handler_that_fails)
    op = _op(writer_keys=[])

    with pytest.raises(RuntimeError, match="a post-apply handler failed") as failed:
        await lifecycle._apply_fleet_op(op, deadline=time.monotonic() + 30)
    await _carried(lifecycle)
    assert seen == [_CHANGE]

    class _Sender(FakeBus):
        async def publish(self, *_args: Any, **_kwargs: Any) -> FleetResult:
            error = f"{type(failed.value).__name__}: {failed.value}"
            return FleetResult(
                op="carry_roll", results=[WorkerResult(name=_MEMBER, outcome=OpOutcome.failed, error=error)]
            )

    with pytest.raises(RuntimeError) as raised:
        await roll_handoff._send_to_member(cast("Any", _Sender(origin="x")), op, _MEMBER)

    message = str(raised.value)
    assert "The roll did not start" not in message
    assert f"Whether {_MEMBER!r} started the roll is unknown" in message
    assert "RuntimeError: a post-apply handler failed" in message
