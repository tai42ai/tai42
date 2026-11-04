"""A change run under a bound hands its fleet recycle roll to the bus member it acts for, which carries it out.

:func:`~tai42_skeleton.config.recycle_roll.decide_and_roll` decides as always; inside a bounded
run (:func:`~tai42_kit.utils.run_bound.bounded_run`: a dispatched job, an armed turn budget) a
decision with a roll to run — a target to roll over, or only the writer's own restart, whose exit
waits for the worker's other changes — is not rolled there but handed to the member through the
``carry_roll`` op — over the bus from a forked job child, to the member's own app in a member
process — and the change answers with the roll deferred to that member. A hand-off that fails
raises, naming the member and the change, whether the roll started, and whether the reload
reached the bus. Every decision outside a bounded run rolls
where it is decided.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import pytest
from redis.exceptions import ResponseError
from tai42_kit.backend.job_run import dispatched_job
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.run_bound import bounded_run

# ``bus_settings`` is imported for its registration side effect: TAI_BUS_* are recycle-class.
import tai42_skeleton.app.bus_settings  # noqa: F401
from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import (
    FleetResult,
    LocalApplyResult,
    OpOutcome,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerResult,
)
from tai42_skeleton.app.bus.ops import FLEET_OP_ADAPTER, CarryRoll, FleetOp
from tai42_skeleton.app.recycle import RecycleReport
from tai42_skeleton.app.writer_exit import writer_exit_scope
from tai42_skeleton.config.recycle_roll import RollFailedError, decide_and_roll
from tai42_skeleton.config.roll_handoff import RollHandOffUnknownError
from tai42_skeleton.tools.turn_budget import turn_budget

from .._fakes.bus import FakeBus

_KEY = "TAI_BUS_HEARTBEAT_TTL"
_CHANGE = "this config change"
_MEMBER = "backend-writer"


def _report(*, sibling_keys: list[str], writer_keys: list[str] | None = None) -> FleetResult:
    return FleetResult(
        op="reload_config",
        results=[
            WorkerResult(name=_MEMBER, outcome=OpOutcome.applied, payload={"awaiting_respawn": writer_keys or []}),
            WorkerResult(name="serve-b", outcome=OpOutcome.applied, payload={"awaiting_respawn": sibling_keys}),
        ],
    )


class _ChildBus(FakeBus):
    """A forked job child's bus: a non-member acting for ``backend-writer``, whose answer to the op is set."""

    def __init__(
        self, *, answer: WorkerResult | None = None, reachable: bool = True, raises: BaseException | None = None
    ) -> None:
        super().__init__(origin=f"{_MEMBER}/fork-9", remotes=["serve-b"])
        self._identity = WorkerIdentity(
            name=f"{_MEMBER}/fork-9", kind=WorkerKind.backend, pid=9, generation=0, member=False
        )
        self._member = WorkerIdentity(name=_MEMBER, kind=WorkerKind.backend, pid=1, generation=3)
        self._answer = answer
        self._reachable = reachable
        self._raises = raises

    async def publish(
        self,
        op: FleetOp,
        targets: list[str] | None,
        local: LocalApplyResult | None,
        *,
        expected_at_start: dict[str, int] | None = None,
    ) -> FleetResult:
        await super().publish(op, targets, local, expected_at_start=expected_at_start)
        if self._raises is not None:
            raise self._raises
        if not self._reachable:
            return FleetResult(op=op.op, reachable=False, error="ConnectionError: refused")
        return FleetResult(op=op.op, results=[] if self._answer is None else [self._answer])


def _carrying() -> WorkerResult:
    return WorkerResult(name=_MEMBER, outcome=OpOutcome.applied, payload={"carrying": _CHANGE})


def _never_rolls(seen: list[str]) -> Any:
    async def orchestrate(_bus: Any, **_kwargs: Any) -> RecycleReport:
        seen.append("rolled")
        return RecycleReport()

    return orchestrate


async def _decide(
    bus: FakeBus,
    report: FleetResult,
    orchestrate: Any,
    *,
    addressed: set[str] | None = None,
    holds: tuple[str, ...] = (),
) -> Any:
    return await decide_and_roll(
        cast("WorkerBus", bus),
        report,
        bus.member_identity,
        {"serve-b": (WorkerKind.serve, 1)},
        change=_CHANGE,
        orchestrate=orchestrate,
        addressed=addressed,
        holds=holds,
    )


async def test_a_bounded_child_hands_a_roll_with_a_target_to_its_member_and_answers_deferred() -> None:
    bus = _ChildBus(answer=_carrying())
    seen: list[str] = []
    report = _report(sibling_keys=[_KEY], writer_keys=[_KEY])

    with writer_exit_scope() as exits, bounded_run():
        outcome = await _decide(bus, report, _never_rolls(seen), addressed={"serve-b", _MEMBER}, holds=("a-roll-hold",))
        await exits.settle()

    assert seen == []
    assert outcome.deferred_to == _MEMBER
    assert outcome.recycle is None
    assert outcome.awaiting_respawn == []
    assert outcome.self_exit_armed is False
    # The job's own scope owes nothing: no exit is asked of the member for the job.
    assert bus.exit_requests == []
    ((wire, targets, local),) = bus.publish_calls
    assert targets == [_MEMBER]
    assert local is None
    op = FLEET_OP_ADAPTER.validate_python(wire)
    assert isinstance(op, CarryRoll)
    assert op.change == _CHANGE
    assert op.report == report
    assert op.pinned == {"serve-b": (WorkerKind.serve, 1)}
    assert op.writer.name == _MEMBER
    assert op.addressed == ("backend-writer", "serve-b")
    assert op.writer_exits is True
    # The holds the change names ride on the op to the member that carries the roll.
    assert op.holds == ("a-roll-hold",)


async def test_a_bounded_member_hands_the_roll_to_its_own_app(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus(origin=_MEMBER, remotes=["serve-b"])
    given: list[CarryRoll] = []

    class _App:
        async def carry_roll(self, op: CarryRoll) -> dict[str, str]:
            given.append(op)
            return {"carrying": op.change}

    monkeypatch.setattr(instance, "_app", _App())
    seen: list[str] = []

    with writer_exit_scope(), bounded_run():
        outcome = await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls(seen))

    assert seen == []
    assert outcome.deferred_to == _MEMBER
    assert [op.change for op in given] == [_CHANGE]
    assert bus.publish_calls == []


@pytest.fixture
def turn_timeout(monkeypatch: pytest.MonkeyPatch) -> Any:
    def _set(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("TAI_TURN_TIMEOUT_SECONDS", raising=False)
        else:
            monkeypatch.setenv("TAI_TURN_TIMEOUT_SECONDS", value)
        reset_all_settings()

    yield _set
    reset_all_settings()


@pytest.mark.parametrize(("budget", "deferred_to"), [("600", _MEMBER), (None, None)], ids=["armed", "unset"])
async def test_a_change_in_a_live_callers_turn_hands_its_roll_off_only_under_an_armed_turn_budget(
    monkeypatch: pytest.MonkeyPatch, turn_timeout: Any, budget: str | None, deferred_to: str | None
) -> None:
    # Every live-caller door (MCP, ``tai tools run``, an agent run, a preset) runs its turn
    # through ``turn_budget``: an armed budget bounds the turn, so its change hands the roll to
    # the member it runs in; with no budget set the turn is unbounded and rolls where it decides.
    turn_timeout(budget)
    bus = FakeBus(origin=_MEMBER, remotes=["serve-b"])

    async def carry_roll(op: CarryRoll) -> dict[str, str]:
        return {"carrying": op.change}

    monkeypatch.setattr(instance.build_app(), "carry_roll", carry_roll)
    seen: list[str] = []

    with writer_exit_scope():
        async with turn_budget():
            outcome = await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls(seen))

    assert outcome.deferred_to == deferred_to
    assert seen == ([] if deferred_to else ["rolled"])


async def test_a_bounded_child_hands_a_roll_with_only_the_writer_to_restart_to_its_member() -> None:
    bus = _ChildBus(answer=_carrying())
    seen: list[str] = []

    with writer_exit_scope() as exits, bounded_run():
        outcome = await _decide(bus, _report(sibling_keys=[], writer_keys=[_KEY]), _never_rolls(seen))
        await exits.settle()

    assert seen == []
    assert outcome.deferred_to == _MEMBER
    assert outcome.self_exit_armed is False
    # The member departs itself once it carried the roll: no exit is asked of it for the job.
    assert bus.exit_requests == []
    ((wire, targets, _local),) = bus.publish_calls
    assert targets == [_MEMBER]
    op = FLEET_OP_ADAPTER.validate_python(wire)
    assert isinstance(op, CarryRoll)
    assert op.writer_exits is True


@asynccontextmanager
async def _under_turn_budget(turn_timeout: Any) -> AsyncIterator[None]:
    turn_timeout("0.2")
    async with turn_budget():
        yield


@asynccontextmanager
async def _under_job_budget(_turn_timeout: Any) -> AsyncIterator[None]:
    # An in-process backend job runs its tool in ``dispatched_job`` under the backend's job budget.
    with dispatched_job():
        async with asyncio.timeout(0.2):
            yield


@pytest.mark.parametrize("bound", [_under_turn_budget, _under_job_budget], ids=["turn-budget", "job-budget"])
async def test_a_bounded_member_hands_off_a_writer_only_roll_so_its_exit_never_waits_under_the_bound(
    monkeypatch: pytest.MonkeyPatch, turn_timeout: Any, bound: Any
) -> None:
    # Only the writer needs its restart, and another change on this worker holds its exit: the
    # exit waits for that change, so it must be carried by the member, outside the bound.
    bus = FakeBus(origin=_MEMBER, remotes=["serve-b"])
    given: list[CarryRoll] = []

    async def carry_roll(op: CarryRoll) -> dict[str, str]:
        given.append(op)
        return {"carrying": op.change}

    monkeypatch.setattr(instance.build_app(), "carry_roll", carry_roll)
    other = bus.defer_exit("another change in flight")
    seen: list[str] = []

    async with bound(turn_timeout):
        with writer_exit_scope() as exits:
            outcome = await _decide(bus, _report(sibling_keys=[], writer_keys=[_KEY]), _never_rolls(seen))
            await exits.settle()

    assert seen == []
    assert outcome.deferred_to == _MEMBER
    assert [op.change for op in given] == [_CHANGE]
    assert bus.exit_requests == []
    assert bus.deferred_exits == ["another change in flight"]
    bus.release_deferred_exit(other)


async def test_a_roll_outside_a_bounded_run_rolls_where_it_is_decided() -> None:
    bus = _ChildBus(answer=_carrying())
    seen: list[str] = []

    with writer_exit_scope():
        outcome = await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls(seen))

    assert seen == ["rolled"]
    assert outcome.deferred_to is None
    assert bus.publish_calls == []


@pytest.mark.parametrize(
    ("bus", "why"),
    [
        (
            _ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.recycling)),
            "it is leaving (recycling) and carries nothing",
        ),
        (
            _ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.applied, payload={"already_leaving": "x"})),
            "it is leaving (x) and carries nothing",
        ),
    ],
    ids=["recycling", "already-leaving"],
)
async def test_a_refused_hand_off_raises_naming_the_member_and_that_the_roll_did_not_start(
    bus: _ChildBus, why: str
) -> None:
    seen: list[str] = []

    with writer_exit_scope(), bounded_run(), pytest.raises(RuntimeError) as raised:
        await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls(seen))

    message = str(raised.value)
    assert f"could not be handed to {_MEMBER!r}" in message
    assert why in message
    assert _CHANGE in message
    assert message.endswith("The roll did not start; the reload broadcast reached the bus")
    assert seen == []


async def test_a_member_found_leaving_raises_one_text_whichever_answer_says_so() -> None:
    # A member that is leaving answers a carried roll ``already_leaving`` when the op reached it,
    # and its row reads ``recycling`` when the op did not: one state, so one text naming why.
    raised: list[str] = []
    for bus in (
        _ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.applied, payload={"already_leaving": "x"})),
        _ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.recycling, detail="x")),
    ):
        with writer_exit_scope(), bounded_run(), pytest.raises(RuntimeError) as error:
            await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls([]))
        raised.append(str(error.value))

    assert raised[0] == raised[1]
    leaving = (
        "it is leaving (x) and carries nothing; a member that replaces it rolls on its join what that join's "
        "report states, and with no replacement nothing rolls"
    )
    assert leaving in raised[0]


@pytest.mark.parametrize(
    ("bus", "why"),
    [
        (_ChildBus(reachable=False), "the bus is unreachable"),
        (_ChildBus(answer=None), "it did not answer"),
        (
            _ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.failed, error="ValueError: bad op")),
            "the op failed there: ValueError: bad op",
        ),
        (_ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.timed_out)), "no answer came: timed_out"),
        (_ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.missing)), "no answer came: missing"),
        (_ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.departed)), "no answer came: departed"),
        (_ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.resyncing)), "no answer came: resyncing"),
        (_ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.stale)), "no answer came: stale"),
        (
            _ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.applied, payload={"status": "ok"})),
            "its answer names no carried roll",
        ),
        (
            _ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.applied, payload={"carrying": 5})),
            "its answer is malformed",
        ),
    ],
    ids=[
        "unreachable",
        "no-row",
        "failed",
        "timed-out",
        "missing",
        "departed",
        "resyncing",
        "stale",
        "not-a-carry-reply",
        "malformed",
    ],
)
async def test_a_hand_off_whose_answer_does_not_tell_raises_that_whether_the_roll_started_is_unknown(
    bus: _ChildBus, why: str
) -> None:
    seen: list[str] = []

    with writer_exit_scope(), bounded_run(), pytest.raises(RuntimeError) as raised:
        await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls(seen))

    message = str(raised.value)
    assert f"may have been sent to {_MEMBER!r}" in message
    assert why in message
    assert _CHANGE in message
    assert f"Whether {_MEMBER!r} started the roll is unknown; the reload broadcast reached the bus." in message
    assert "did not start" not in message
    assert seen == []
    # The roll may have been sent to the member, which may carry it: the outcome defers it there, unjudged.
    assert isinstance(raised.value, RollFailedError)
    assert isinstance(raised.value.failure, RollHandOffUnknownError)
    assert raised.value.failure.member == _MEMBER
    outcome = raised.value.outcome
    assert (outcome.deferred_to, outcome.recycle, outcome.awaiting_respawn, outcome.self_exit_armed) == (
        _MEMBER,
        None,
        [],
        False,
    )


async def test_a_carry_publish_that_raises_reads_as_a_hand_off_whose_answer_is_unknown() -> None:
    # A Redis error reply raised from the publish may come after the op went out: the member may
    # carry the roll, so the hand-off raises that its answer is not known, chained to the error.
    error = ResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")
    bus = _ChildBus(raises=error)
    seen: list[str] = []

    with writer_exit_scope(), bounded_run(), pytest.raises(RollFailedError) as raised:
        await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls(seen))

    unknown = raised.value.failure
    assert isinstance(unknown, RollHandOffUnknownError)
    assert unknown.member == _MEMBER
    assert unknown.__cause__ is error
    assert str(unknown) == (
        f"fleet recycle: the roll for {_CHANGE} may have been sent to {_MEMBER!r}, the bus member the change acts "
        "for, but "
        "its answer is not known — the bus failed: ResponseError: WRONGTYPE Operation against a key holding the "
        f"wrong kind of value. Whether {_MEMBER!r} started the roll is unknown; the reload broadcast reached the "
        "bus. Read its log and the fleet census"
    )
    assert seen == []
    outcome = raised.value.outcome
    assert (outcome.deferred_to, outcome.recycle, outcome.awaiting_respawn) == (_MEMBER, None, [])


async def test_a_carry_publish_cancelled_propagates_the_cancellation() -> None:
    bus = _ChildBus(raises=asyncio.CancelledError())

    with writer_exit_scope(), bounded_run(), pytest.raises(asyncio.CancelledError):
        await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls([]))


async def test_a_bounded_member_with_no_app_built_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = FakeBus(origin=_MEMBER, remotes=["serve-b"])
    monkeypatch.setattr(instance, "_app", None)

    with writer_exit_scope(), bounded_run(), pytest.raises(RuntimeError, match="its app is not built"):
        await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls([]))


async def test_a_bounded_member_whose_app_does_not_take_the_op_raises_naming_the_member_and_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bus = FakeBus(origin=_MEMBER, remotes=["serve-b"])

    class _App:
        async def carry_roll(self, op: CarryRoll) -> dict[str, str]:
            raise LookupError("no roll hold is registered under 'a-roll-hold'")

    monkeypatch.setattr(instance, "_app", _App())

    with writer_exit_scope(), bounded_run(), pytest.raises(RuntimeError) as raised:
        await _decide(bus, _report(sibling_keys=[_KEY]), _never_rolls([]), holds=("a-roll-hold",))

    message = str(raised.value)
    assert repr(_MEMBER) in message
    assert _CHANGE in message
    assert "its app did not take the op: LookupError: no roll hold is registered under 'a-roll-hold'" in message
    # The roll did not start, so it is judged here from the reload's report: serve-b still awaits.
    assert isinstance(raised.value, RollFailedError)
    assert raised.value.outcome.deferred_to is None
    assert [worker.name for worker in raised.value.outcome.awaiting_respawn] == ["serve-b"]


_UNREACHED = FleetResult(op="reload_config", reachable=False, error="ConnectionError: refused")


@pytest.mark.parametrize(
    ("bus", "state"),
    [
        (
            _ChildBus(reachable=False),
            f"the bus is unreachable: ConnectionError: refused. Whether {_MEMBER!r} started the roll is unknown; "
            "the reload broadcast did not reach the bus. Read its log and the fleet census",
        ),
        (
            _ChildBus(answer=WorkerResult(name=_MEMBER, outcome=OpOutcome.recycling)),
            "it is leaving (recycling) and carries nothing; a member that replaces it rolls on its join what that "
            "join's report states, and with no replacement nothing rolls. The roll did not start; the reload "
            "broadcast did not reach the bus",
        ),
    ],
    ids=["unknown", "not-handed"],
)
async def test_a_failed_hand_off_of_a_reload_that_did_not_reach_the_bus_never_says_the_fleet_reloaded(
    bus: _ChildBus, state: str
) -> None:
    # A reload whose broadcast never reached the bus reloaded no sibling: the hand-off's error says
    # so from the reload's own report, whichever way the hand-off failed.
    with writer_exit_scope(), bounded_run(), pytest.raises(RuntimeError) as raised:
        await decide_and_roll(
            cast("WorkerBus", bus),
            _UNREACHED,
            bus.member_identity,
            {},
            change=_CHANGE,
            orchestrate=_never_rolls([]),
            writer_exits=False,
            addressed={"backend-1"},
        )

    message = str(raised.value)
    assert message.endswith(state)
    assert "the fleet reloaded" not in message
    assert "persisted" not in message
