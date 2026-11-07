"""The member a bounded change hands its roll to holds it until the run released it, then carries it out last.

The ``carry_roll`` op (over the bus, or given to the member's own app) is answered once the
member took the roll holds the op names: a member already leaving carries nothing; otherwise it
holds its own exit for the change from the answer on, so a ``Recycle`` op from another worker's
roll is deferred to it, and keeps the roll held as a tracked task. The run that handed the roll
off releases it once it ended (the ``release_roll`` op, or the in-process release
onto the serving loop), and only then does the roll start; a roll handed over the bus starts by
the run's bound (``answers_within``) at the latest, with an ERROR line. A hold the member cannot
take leaves the op unapplied with nothing carried. A converged roll whose writer needs its own
restart departs the member once the roll ended; any other roll releases the hold and departs
nothing. Shutdown cancels the task, logged at ERROR.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest
from tai42_contract.app import tai42_app
from tai42_kit.utils.run_bound import bounded_run

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
_ROLL = "roll-1"


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


def _op(
    *, writer_keys: list[str], writer_exits: bool = True, holds: tuple[str, ...] = (), answers_within: float = 30.0
) -> CarryRoll:
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
        roll=_ROLL,
        answers_within=answers_within,
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


async def _settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def _accept_and_release(lifecycle: _Lifecycle, op: CarryRoll) -> dict[str, str]:
    """Accept ``op`` as the in-process door does, then release it as its run's bound exit does; return the answer."""
    answer = await lifecycle.accept_carried_roll(op, answers_within=None)
    if op.roll in lifecycle._held_rolls:
        lifecycle._release_carried_roll(op.roll, op.change)
    return answer


async def test_the_member_answers_holds_the_roll_until_released_and_departs_last_after_a_converged_roll(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)

    answer = await lifecycle._dispatch_bus_op(_op(writer_keys=[_KEY]))

    assert answer == {"carrying": _CHANGE}
    # Answered, and held: the roll waits for the run's release, the member's exit held for the change.
    await _settle()
    assert seen == []
    assert bus.exit_deferred == _CHANGE
    assert lifecycle._release_carried_roll(_ROLL, _CHANGE) == {"started": _CHANGE}
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

    await _accept_and_release(lifecycle, _op(writer_keys=writer_keys, writer_exits=writer_exits))
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
        await _accept_and_release(lifecycle, _op(writer_keys=[_KEY]))
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

    await _accept_and_release(lifecycle, _op(writer_keys=[]))
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
    await _accept_and_release(lifecycle, _op(writer_keys=[_KEY]))
    await asyncio.wait_for(rolling.wait(), timeout=5)

    with caplog.at_level(logging.ERROR):
        await lifecycle._cancel_bus_subscription()

    assert any("cancelled mid-roll" in record.getMessage() for record in caplog.records)
    assert lifecycle._carried_roll_tasks == set()
    assert bus.exit_requests == []
    assert bus.exit_deferred is None


async def test_shutdown_cancels_a_held_roll_gives_its_exit_hold_back_and_logs_it(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    lifecycle, bus = member
    rolled: list[str | None] = []
    _rolls(monkeypatch, rolled, bus)

    await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY]), answers_within=None)
    # The answer was given and the roll is held, waiting for a release that has not come.
    await _settle()
    assert bus.exit_deferred == _CHANGE
    with caplog.at_level(logging.ERROR):
        await lifecycle._cancel_bus_subscription()

    assert rolled == []
    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert any(_CHANGE in message and "before it was released" in message for message in messages)
    assert lifecycle._held_rolls == {}
    assert bus.exit_requests == []
    assert bus.exit_deferred is None


async def test_a_carried_roll_cancelled_before_its_first_step_fails_the_answer_waiting_on_its_holds(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch
) -> None:
    lifecycle, bus = member
    _rolls(monkeypatch, [], bus)

    answer = asyncio.create_task(lifecycle.accept_carried_roll(_op(writer_keys=[_KEY]), answers_within=None))
    # The answer spawned the carried task and waits on its holds; the task has not run a step yet.
    await asyncio.sleep(0)
    assert len(lifecycle._carried_roll_tasks) == 1
    await lifecycle._cancel_bus_subscription()

    with pytest.raises(RuntimeError, match="stopped before it took the holds"):
        await asyncio.wait_for(answer, timeout=5)
    assert lifecycle._carried_roll_tasks == set()
    assert bus.exit_requests == []
    assert bus.exit_deferred is None


async def test_an_acceptance_cancelled_once_the_holds_are_taken_keeps_the_roll_and_the_release_starts_it(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The caller is cancelled in the step the carried task took the holds: the answer reads its
    # cancellation, and the roll stays held for the run's release, which starts it.
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)
    lifecycle.bind_serving_loop()
    callers: list[asyncio.Task[dict[str, str]]] = []
    released = carried_roll_module._released

    async def cancelling(roll: Any, answers_within: float | None) -> None:
        callers[0].cancel()
        await released(roll, answers_within)

    monkeypatch.setattr(carried_roll_module, "_released", cancelling)
    op = _op(writer_keys=[])

    with caplog.at_level(logging.ERROR):
        callers.append(asyncio.create_task(lifecycle.carry_roll(op)))
        with pytest.raises(asyncio.CancelledError):
            await callers[0]
        await _settle()
        assert op.roll in lifecycle._held_rolls
        assert lifecycle._release_carried_roll(op.roll, op.change) == {"started": _CHANGE}
        await _carried(lifecycle)

    assert seen == [_CHANGE]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


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
    lifecycle._release_carried_roll(_ROLL, _CHANGE)
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
        await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY], holds=("first", refuse)), answers_within=None)

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

    answer = await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY]), answers_within=None)

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
        answers.append(await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY]), answers_within=None))

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
        answers.append(await lifecycle.accept_carried_roll(_op(writer_keys=[_KEY]), answers_within=None))

    monkeypatch.setattr(bus, "mark_recycling", mark_recycling)

    assert await lifecycle._apply_recycle() == {"recycling": "backend"}

    assert answers == [{"already_leaving": "a fleet recycle op"}]
    assert lifecycle._carried_roll_tasks == set()
    assert bus.exit_deferred is None
    assert seen == []


async def test_a_change_in_the_member_off_its_serving_loop_hands_and_releases_the_roll_onto_that_loop(
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
        # A job on a backend worker's private task loop, in another thread: it hands the roll
        # off, then its bound's exit releases it.
        op = _op(writer_keys=[])
        answers.append(asyncio.run(lifecycle.carry_roll(op)))
        lifecycle.release_carried_roll(op.roll, op.change)

    thread = threading.Thread(target=off_loop)
    thread.start()
    await asyncio.to_thread(thread.join, 5)
    await _carried(lifecycle)

    assert answers == [{"carrying": _CHANGE}]
    assert loops == [serving]


async def test_a_run_off_the_serving_loop_cancelled_after_the_member_accepted_is_released_at_its_bounds_exit(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A job on a backend worker's private task loop hands its roll to the member it runs in; the
    # serving loop accepted it, and the job was cut before it read the answer. Its bound's exit
    # releases the roll onto the serving loop, which starts it.
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)
    # The member's own app takes the op; the roll's drain budgets stay those of the built app.
    monkeypatch.setattr(instance, "built_app", lambda: lifecycle)
    lifecycle.bind_serving_loop()
    callers: list[asyncio.Task[None]] = []
    ends: list[str] = []
    # The job's loop is held from the moment the holds are taken until its cut is queued behind
    # the answer's arrival, so the job reads the cut before it can read the answer.
    job_loop_held = threading.Event()
    job_loop_may_run = threading.Event()

    def hold_the_job_loop() -> None:
        job_loop_held.set()
        job_loop_may_run.wait(5)

    taken = asyncio.Event()
    released = carried_roll_module._released

    async def taking(roll: Any, answers_within: float | None) -> None:
        callers[0].get_loop().call_soon_threadsafe(hold_the_job_loop)
        job_loop_held.wait(5)
        taken.set()
        await released(roll, answers_within)

    monkeypatch.setattr(carried_roll_module, "_released", taking)
    op = _op(writer_keys=[])

    async def job() -> None:
        async def change() -> None:
            async with bounded_run(60) as bound:
                await roll_handoff.hand_off_roll(
                    cast("Any", bus),
                    op.report,
                    op.writer,
                    op.pinned,
                    bound=bound,
                    change=_CHANGE,
                    writer_exits=False,
                    addressed=None,
                )

        caller = asyncio.create_task(change())
        callers.append(caller)
        try:
            await caller
        except asyncio.CancelledError:
            ends.append("cancelled")

    before = asyncio.all_tasks()
    thread = threading.Thread(target=lambda: asyncio.run(job()))
    with caplog.at_level(logging.INFO):
        thread.start()
        await asyncio.wait_for(taken.wait(), 5)
        (roll,) = lifecycle._held_rolls.values()
        hop = next(task for task in asyncio.all_tasks() - before if task not in lifecycle._carried_roll_tasks)
        # The acceptance answered; its answer is queued on the held job loop, the cut right behind it.
        await asyncio.wait({hop})
        callers[0].get_loop().call_soon_threadsafe(callers[0].cancel)
        job_loop_may_run.set()
        await asyncio.to_thread(thread.join, 5)
        await _settle()
        assert ends == ["cancelled"]
        assert roll.released.is_set()
        await _carried(lifecycle)

    assert seen == [_CHANGE]
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]
    messages = [record.getMessage() for record in caplog.records]
    assert f"the run that handed this worker the roll for {_CHANGE} ended: the roll starts" in messages


_ROLL_STARTS = f"the run that handed this worker the roll for {_CHANGE} ended: the roll starts"
_NO_ROLL_HELD = (
    f"fleet recycle: the run that handed this worker the roll for {_CHANGE} ended, but its release found no roll "
    "held here — the roll was given up before it started (the acceptance was cancelled before the holds were taken, "
    "or this worker is stopping); it did not start"
)
_STOPPED_WHILE_HELD = (
    f"the fleet recycle for {_CHANGE} handed to this worker was cancelled before it was released (the worker is "
    "stopping) — no target was rolled"
)


class _GatedHold:
    """The roll hold ``gated``: its entry passes once the test lets it, and its exit waits for the test.

    ``reached`` is set once the entry began, ``exiting`` once the exit began; the exit runs on
    every ending, the entry cancelled included, and returns only once ``exit_may_run`` is set,
    so a release can reach the member while the carried task is still unwinding.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, entry_passes: bool) -> None:
        self.reached = asyncio.Event()
        self.entry_may_pass = asyncio.Event()
        self.exiting = asyncio.Event()
        self.exit_may_run = asyncio.Event()
        if entry_passes:
            self.entry_may_pass.set()
        monkeypatch.setattr(roll_holds, "_HOLDS", {"gated": self._hold})

    @asynccontextmanager
    async def _hold(self) -> AsyncIterator[None]:
        try:
            self.reached.set()
            await self.entry_may_pass.wait()
            yield
        finally:
            self.exiting.set()
            await self.exit_may_run.wait()


def _released_in_this_process(monkeypatch: pytest.MonkeyPatch, lifecycle: _Lifecycle) -> asyncio.Event:
    """Return an event set on the serving loop once an in-process release of a roll was processed there."""
    processed = asyncio.Event()
    release = lifecycle._release_in_this_process

    def releasing(roll: str, change: str) -> None:
        release(roll, change)
        processed.set()

    monkeypatch.setattr(lifecycle, "_release_in_this_process", releasing)
    return processed


async def _carried_tasks_ended(lifecycle: _Lifecycle) -> None:
    """Wait for every carried task to end, however it ends, and for its done-callbacks to have run."""
    tasks = set(lifecycle._carried_roll_tasks)
    if tasks:
        _done, pending = await asyncio.wait(tasks, timeout=5)
        assert not pending
    assert lifecycle._carried_roll_tasks == set()


def _handing_off_under_a_bound(bus: FakeBus, *, holds: tuple[str, ...]) -> Callable[[], Coroutine[Any, Any, None]]:
    """A change that hands its roll, naming ``holds``, to the member it runs in, under its bound."""
    op = _op(writer_keys=[])

    async def change() -> None:
        async with bounded_run(60) as bound:
            await roll_handoff.hand_off_roll(
                cast("Any", bus),
                op.report,
                op.writer,
                op.pinned,
                bound=bound,
                change=_CHANGE,
                writer_exits=False,
                addressed=None,
                holds=holds,
            )

    return change


async def test_a_run_on_the_serving_loop_cut_before_the_member_took_the_holds_logs_that_no_roll_started(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A turn on the member's serving loop hands its roll to its own app and is cut while the
    # carried task takes the holds: the roll is given up, and the release the bound's exit
    # sends reaches the serving loop while the hold is still unwinding.
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)
    monkeypatch.setattr(instance, "built_app", lambda: lifecycle)
    lifecycle.bind_serving_loop()
    gated = _GatedHold(monkeypatch, entry_passes=False)
    processed = _released_in_this_process(monkeypatch, lifecycle)
    change = _handing_off_under_a_bound(bus, holds=("gated",))

    with caplog.at_level(logging.INFO):
        caller = asyncio.create_task(change())
        await asyncio.wait_for(gated.reached.wait(), 5)
        caller.cancel()
        await asyncio.wait({caller}, timeout=5)
        assert caller.cancelled()
        await asyncio.wait_for(processed.wait(), 5)
        gated.exit_may_run.set()
        await _carried_tasks_ended(lifecycle)

    assert seen == []
    assert lifecycle._held_rolls == {}
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR] == [_NO_ROLL_HELD]
    assert _ROLL_STARTS not in [record.getMessage() for record in caplog.records]


async def test_a_run_off_the_serving_loop_cut_before_the_member_took_the_holds_logs_that_no_roll_started(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A job on a backend worker's private task loop hands its roll to the member it runs in and
    # is cut while the serving loop's carried task takes the holds; the hold's exit waits until
    # the release the job's bound sent at its exit was processed on the serving loop.
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)
    monkeypatch.setattr(instance, "built_app", lambda: lifecycle)
    lifecycle.bind_serving_loop()
    gated = _GatedHold(monkeypatch, entry_passes=False)
    processed = _released_in_this_process(monkeypatch, lifecycle)
    change = _handing_off_under_a_bound(bus, holds=("gated",))
    callers: list[asyncio.Task[None]] = []
    ends: list[str] = []

    async def job() -> None:
        caller = asyncio.create_task(change())
        callers.append(caller)
        try:
            await caller
        except asyncio.CancelledError:
            ends.append("cancelled")

    thread = threading.Thread(target=lambda: asyncio.run(job()))
    with caplog.at_level(logging.INFO):
        thread.start()
        await asyncio.wait_for(gated.reached.wait(), 5)
        callers[0].get_loop().call_soon_threadsafe(callers[0].cancel)
        await asyncio.wait_for(processed.wait(), 5)
        gated.exit_may_run.set()
        await asyncio.to_thread(thread.join, 5)
        assert ends == ["cancelled"]
        await _carried_tasks_ended(lifecycle)

    assert seen == []
    assert lifecycle._held_rolls == {}
    assert [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR] == [_NO_ROLL_HELD]
    assert _ROLL_STARTS not in [record.getMessage() for record in caplog.records]


async def test_a_release_reaching_a_worker_stopping_while_it_held_the_roll_logs_that_no_roll_started(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # The member holds the roll, waiting for its release, when the worker stops; the run's
    # in-process release reaches the serving loop while the holds are still unwinding.
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)
    lifecycle.bind_serving_loop()
    gated = _GatedHold(monkeypatch, entry_passes=True)
    processed = _released_in_this_process(monkeypatch, lifecycle)
    op = _op(writer_keys=[], holds=("gated",))

    with caplog.at_level(logging.INFO):
        answer = await asyncio.wait_for(lifecycle.accept_carried_roll(op, answers_within=None), 5)
        assert answer == {"carrying": _CHANGE}
        stopping = asyncio.create_task(lifecycle._cancel_carried_rolls())
        await asyncio.wait_for(gated.exiting.wait(), 5)
        lifecycle.release_carried_roll(op.roll, op.change)
        await asyncio.wait_for(processed.wait(), 5)
        gated.exit_may_run.set()
        await asyncio.wait_for(stopping, 5)

    assert seen == []
    assert lifecycle._held_rolls == {}
    assert lifecycle._carried_roll_tasks == set()
    errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors == [_STOPPED_WHILE_HELD, _NO_ROLL_HELD]
    assert _ROLL_STARTS not in [record.getMessage() for record in caplog.records]


class _ChildSender(FakeBus):
    """A forked job child acting for the member ``lifecycle``: each op it publishes is applied there.

    The op is applied as the member's bus subscription does (:meth:`_apply_fleet_op`, post-apply
    handlers included), and a raise there reads as the op's ``failed`` row.
    """

    def __init__(self, lifecycle: _Lifecycle) -> None:
        super().__init__(origin=f"{_MEMBER}/fork-9", remotes=["serve-b"])
        self._identity = WorkerIdentity(
            name=f"{_MEMBER}/fork-9", kind=WorkerKind.backend, pid=9, generation=0, member=False
        )
        self._member = WorkerIdentity(name=_MEMBER, kind=WorkerKind.backend, pid=1, generation=3)
        self._lifecycle = lifecycle

    async def publish(self, op: Any, *_args: Any, **_kwargs: Any) -> FleetResult:
        try:
            payload = await self._lifecycle._apply_fleet_op(op, deadline=time.monotonic() + 30)
        except Exception as error:
            row = WorkerResult(name=_MEMBER, outcome=OpOutcome.failed, error=f"{type(error).__name__}: {error}")
        else:
            row = WorkerResult(name=_MEMBER, outcome=OpOutcome.applied, payload=payload)
        return FleetResult(op=op.op, results=[row])


async def test_a_carry_op_failed_by_a_post_apply_handler_after_the_member_took_the_roll_is_released_at_the_exit(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A raising ``on_fleet_op_applied`` handler fails the op once the member took the roll: the
    # sender reads its start as unknown, and its run's bound still releases the roll at its exit,
    # so the roll starts then, never at the run's bound.
    lifecycle, bus = member
    seen: list[str | None] = []
    _rolls(monkeypatch, seen, bus)

    def a_handler_that_fails(applied: Any) -> None:
        if applied.op == "carry_roll":
            raise RuntimeError("a post-apply handler failed")

    lifecycle._on_fleet_op_applied(a_handler_that_fails)
    op = _op(writer_keys=[])

    with caplog.at_level(logging.INFO):
        async with bounded_run(60) as bound:
            with pytest.raises(roll_handoff.RollHandOffUnknownError) as raised:
                await roll_handoff.hand_off_roll(
                    cast("Any", _ChildSender(lifecycle)),
                    op.report,
                    op.writer,
                    op.pinned,
                    bound=bound,
                    change=_CHANGE,
                    writer_exits=False,
                    addressed=None,
                )
            # The member holds the roll until the run ended.
            await _settle()
            assert seen == []
        await _carried(lifecycle)

    message = str(raised.value)
    assert "The roll did not start" not in message
    assert f"Whether {_MEMBER!r} started the roll is unknown" in message
    assert "RuntimeError: a post-apply handler failed" in message
    assert seen == [_CHANGE]
    assert lifecycle._held_rolls == {}
    messages = [record.getMessage() for record in caplog.records]
    assert f"the fleet recycle for {_CHANGE} is released to {_MEMBER!r}, which starts it now" in messages
    assert not [record for record in caplog.records if record.levelno >= logging.ERROR]


async def test_a_bus_door_roll_with_no_release_by_answers_within_starts_with_an_error_line_and_reads_started(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # No release from the run that handed the roll over the bus comes by its bound: the member
    # starts the roll then; a release coming after reads that.
    lifecycle, _bus = member
    rolling = asyncio.Event()
    may_end = asyncio.Event()

    async def orchestrate(_bus: Any, **_kwargs: Any) -> RecycleReport:
        rolling.set()
        await may_end.wait()
        return _recycled()

    monkeypatch.setattr(carried_roll_module, "orchestrate_recycle", orchestrate)

    with caplog.at_level(logging.ERROR):
        assert await lifecycle._dispatch_bus_op(_op(writer_keys=[], answers_within=0.05)) == {"carrying": _CHANGE}
        await asyncio.wait_for(rolling.wait(), timeout=5)

    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert messages == [
        f"the run that handed this worker the roll for {_CHANGE} sent no release by its bound (0.05s) — its job "
        "was cut at its limit or died, or its release was lost; carrying the roll now"
    ]
    assert lifecycle._release_carried_roll(_ROLL, _CHANGE) == {"already_started": _CHANGE}
    may_end.set()
    await _carried(lifecycle)
    assert lifecycle._held_rolls == {}


@pytest.mark.parametrize(("door", "deadline"), [("bus", 30.0), ("in-process", None)])
async def test_only_the_bus_door_arms_a_deadline_on_the_held_roll(
    member: tuple[_Lifecycle, FakeBus], monkeypatch: pytest.MonkeyPatch, door: str, deadline: float | None
) -> None:
    # A run in this process releases its roll at its bound's exit itself, so the in-process door
    # holds the roll with no deadline; a run in another process can end with no word.
    lifecycle, bus = member
    _rolls(monkeypatch, [], bus)
    armed: list[float | None] = []
    released = carried_roll_module._released

    async def recording(roll: Any, answers_within: float | None) -> None:
        armed.append(answers_within)
        await released(roll, answers_within)

    monkeypatch.setattr(carried_roll_module, "_released", recording)
    op = _op(writer_keys=[])
    if door == "bus":
        await lifecycle._dispatch_bus_op(op)
    else:
        lifecycle.bind_serving_loop()
        await lifecycle.carry_roll(op)
    await _settle()

    assert armed == [deadline]
    lifecycle._release_carried_roll(op.roll, op.change)
    await _carried(lifecycle)


async def test_a_release_naming_no_held_roll_raises(member: tuple[_Lifecycle, FakeBus]) -> None:
    lifecycle, _bus = member

    with pytest.raises(LookupError, match=f"holds no roll roll-9 for {_CHANGE} to release"):
        lifecycle._release_carried_roll("roll-9", _CHANGE)


async def test_an_in_process_release_of_a_roll_no_longer_held_is_logged_on_the_serving_loop(
    member: tuple[_Lifecycle, FakeBus], caplog: pytest.LogCaptureFixture
) -> None:
    lifecycle, _bus = member
    lifecycle.bind_serving_loop()

    with caplog.at_level(logging.ERROR):
        lifecycle.release_carried_roll("roll-9", _CHANGE)
        await _settle()

    messages = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert messages == [_NO_ROLL_HELD]


async def test_an_in_process_release_on_a_worker_with_no_serving_loop_raises(
    member: tuple[_Lifecycle, FakeBus],
) -> None:
    lifecycle, _bus = member

    with pytest.raises(RuntimeError, match=f"the roll for {_CHANGE} is released on a worker with no serving loop"):
        lifecycle.release_carried_roll(_ROLL, _CHANGE)
