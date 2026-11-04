"""A departing worker's announced drain extends the roll's wait for it.

A worker leaving gracefully marks itself ``state=recycling`` and announces on its presence row how
long it may take to finish its in-flight work and exit (``drain_seconds``). The roll waits that
drain plus its own start/rejoin step for the life to go and its replacement to join, so a worker
that is only finishing its jobs is not reported timed out. The extension is taken once per life,
from the first sight of it leaving: a life that stays past its announced drain and the step after
it still times out, its detail naming the drain it announced.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

from tai42_skeleton.app.bus import (
    FleetResult,
    OpOutcome,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerResult,
    WorkerRow,
    WorkerState,
)
from tai42_skeleton.app.bus.ops import FleetOp
from tai42_skeleton.app.lifecycle.exit_drain import ExitDrain
from tai42_skeleton.app.recycle import RECYCLED, TIMED_OUT, RecycleReport, RecycleTimeoutError, orchestrate_recycle
from tai42_skeleton.config.recycle_policy import RecycleDecision, ReportedKeys

_NOW = "2026-01-01T00:00:00+00:00"
_TTL = 15.0
_KEY = "RECYCLE_TEST_KEY"
_TARGET = "backend-1"
_STEP = 0.05


def _row(
    *, generation: int = 1, state: WorkerState = WorkerState.ready, drain_seconds: float | None = None
) -> WorkerRow:
    return WorkerRow(
        name=_TARGET,
        kind=WorkerKind.backend,
        pid=1,
        generation=generation,
        joined_at=_NOW,
        beat_at=_NOW,
        state=state,
        deployment_keys=[],
        env_readers="",
        drain_seconds=drain_seconds,
        pttl_ms=int(_TTL * 1000),
    )


class _DrainingBus:
    """One backend target whose row reads ``recycling``, announcing ``drain``, from its departure until its life ends.

    Asked to leave, it announces its departure ``announces_after`` seconds later (until then its row
    still reads ``ready``) and its life ends ``leaves_after`` seconds after the announce; the slot is
    then taken by the next life, ready — unless ``leaves_after`` is ``None``, a life that never
    leaves. ``starts`` is the target's row when the roll begins; when it already reads
    ``recycling`` (a gap row) it is draining from the start. ``seen`` is the state each census
    read, and ``asked_at`` the number of census reads before it was asked to leave.
    """

    def __init__(
        self,
        *,
        drain: float | None,
        leaves_after: float | None,
        starts: WorkerRow | None = None,
        announces_after: float = 0.0,
    ) -> None:
        self._drain = drain
        self._leaves_after = leaves_after
        self._announces_after = announces_after
        self._rows = [starts if starts is not None else _row()]
        self._generation = self._rows[0].generation
        self._announce_at: float | None = None
        self._left_at: float | None = None
        if starts is not None and starts.state is WorkerState.recycling:
            self._start_draining()
        self.published: list[str] = []
        self.seen: list[WorkerState] = []
        self.asked_at: int | None = None

    @property
    def heartbeat_ttl(self) -> float:
        return _TTL

    def _start_draining(self) -> None:
        loop = asyncio.get_running_loop()
        self._left_at = None if self._leaves_after is None else loop.time() + self._leaves_after
        self._rows = [_row(generation=self._generation, state=WorkerState.recycling, drain_seconds=self._drain)]

    async def census(self) -> list[WorkerRow]:
        now = asyncio.get_running_loop().time()
        if self._announce_at is not None and now >= self._announce_at:
            self._announce_at = None
            self._start_draining()
        if self._left_at is not None and now >= self._left_at:
            self._generation += 1
            self._rows = [_row(generation=self._generation)]
            self._left_at = None
        self.seen.extend(row.state for row in self._rows)
        return list(self._rows)

    async def publish(self, op: FleetOp, targets: list[str] | None, local: Any) -> FleetResult:
        assert targets == [_TARGET]
        self.published.append(op.op)
        if op.op == "reload_config":
            # Asked once ready, the target's next life states the key still off its bound value.
            payload = {"status": "ok", "awaiting_respawn": [_KEY]}
            return FleetResult(
                op=op.op, results=[WorkerResult(name=_TARGET, outcome=OpOutcome.applied, payload=payload)]
            )
        self.asked_at = len(self.seen)
        if self._announces_after:
            self._announce_at = asyncio.get_running_loop().time() + self._announces_after
        else:
            self._start_draining()
        return FleetResult(op=op.op, results=[WorkerResult(name=_TARGET, outcome=OpOutcome.applied)])


async def _roll(fake: _DrainingBus) -> RecycleReport:
    decision = RecycleDecision(
        writer=WorkerIdentity(name="serve-1", kind=WorkerKind.serve, pid=1, generation=7),
        reported={_TARGET: ReportedKeys(1, (_KEY,))},
        unconfirmed={},
        clean=frozenset(),
        writer_reason=None,
        withheld={},
        addressed=None,
        report_unreachable=False,
    )
    return await asyncio.wait_for(
        orchestrate_recycle(
            cast("WorkerBus", fake),
            decision=decision,
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=_STEP,
            poll_interval=0.005,
        ),
        timeout=5,
    )


async def test_a_target_finishing_its_jobs_within_its_announced_drain_is_recycled() -> None:
    # It drains for four steps, well inside the drain it announced.
    fake = _DrainingBus(drain=10 * _STEP, leaves_after=4 * _STEP)

    report = await _roll(fake)

    assert [(row.name, row.status) for row in report.rows] == [(_TARGET, RECYCLED)]
    assert report.converged is True


async def test_a_departure_first_seen_on_a_later_census_extends_the_wait_and_converges() -> None:
    # The roll's first polls after the ask still read the life ready; its departure is announced
    # half a step later and its row reads recycling until the life ends four steps after that,
    # past the roll's own step.
    fake = _DrainingBus(drain=10 * _STEP, leaves_after=4 * _STEP, announces_after=_STEP / 2)

    report = await _roll(fake)

    assert fake.asked_at is not None
    first_recycling = fake.seen.index(WorkerState.recycling)
    assert fake.asked_at < first_recycling
    assert set(fake.seen[fake.asked_at : first_recycling]) == {WorkerState.ready}
    assert [(row.name, row.status) for row in report.rows] == [(_TARGET, RECYCLED)]
    assert report.converged is True


async def test_a_target_still_in_its_lifespan_shutdown_after_its_runtime_phase_is_recycled() -> None:
    # Its process declares a runtime stop phase of one step and a lifespan-shutdown bound of three
    # more; it leaves after three steps, past its runtime phase plus the roll's step but inside the
    # drain it announces.
    exit_drain = ExitDrain()
    exit_drain.declare(lambda: _STEP)
    exit_drain.declare_shutdown("module", lambda: 3 * _STEP)
    fake = _DrainingBus(drain=exit_drain.seconds(), leaves_after=3 * _STEP)

    report = await _roll(fake)

    assert [(row.name, row.status) for row in report.rows] == [(_TARGET, RECYCLED)]
    assert report.converged is True


async def test_a_target_that_announced_no_drain_is_waited_only_the_step() -> None:
    fake = _DrainingBus(drain=None, leaves_after=4 * _STEP)

    with pytest.raises(RecycleTimeoutError) as excinfo:
        await _roll(fake)

    assert excinfo.value.unsatisfied == "old life still present"
    assert [(row.name, row.status) for row in excinfo.value.report.rows] == [(_TARGET, TIMED_OUT)]


async def test_a_target_staying_past_its_announced_drain_times_out_naming_the_drain() -> None:
    fake = _DrainingBus(drain=2 * _STEP, leaves_after=None)

    with pytest.raises(RecycleTimeoutError) as excinfo:
        await _roll(fake)

    assert excinfo.value.unsatisfied == "old life still present after its announced drain of 0.1s"
    assert [(row.name, row.status) for row in excinfo.value.report.rows] == [(_TARGET, TIMED_OUT)]


async def test_a_gap_row_target_draining_when_the_roll_reaches_it_is_waited_its_drain_back_to_ready() -> None:
    fake = _DrainingBus(
        drain=10 * _STEP,
        leaves_after=4 * _STEP,
        starts=_row(state=WorkerState.recycling, drain_seconds=10 * _STEP),
    )

    report = await _roll(fake)

    # Its next life came back ready, was asked, and was the one recycled.
    assert fake.published == ["reload_config", "recycle"]
    assert [(row.name, row.status, row.generation_before) for row in report.rows] == [(_TARGET, RECYCLED, 2)]


async def test_a_gap_row_target_staying_past_its_announced_drain_times_out_naming_the_drain() -> None:
    fake = _DrainingBus(
        drain=2 * _STEP,
        leaves_after=None,
        starts=_row(state=WorkerState.recycling, drain_seconds=2 * _STEP),
    )

    with pytest.raises(RecycleTimeoutError) as excinfo:
        await _roll(fake)

    assert excinfo.value.unsatisfied == (
        "gap-row target never returned to ready (last state: recycling) after its announced drain of 0.1s"
    )
    assert fake.published == []
