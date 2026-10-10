"""Recycle orchestration — the rolling per-kind loop, confirmed on REALITY.

Driven against a scripted fake bus so the census/publish sequence is deterministic.
Each recycle publish can transform the census to model what a supervised respawn +
boot resync would do: the target's OLD life ends and NEW ready capacity of the kind
joins. The tests pin the two acceptance facts (old life gone AND counted fresh
capacity), the report shape ({name, kind, generation_before, status} + a per-kind
fresh list, no generation_after / no names-only replacements), the loud timeout that
names the unsatisfied fact, and the slot-name-REUSE case the whole convergence proof
exists to close (a replacement takes the freed name at a higher generation).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError as RedisResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from tai42_contract.errors import ClientDisconnectedError

from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerBus, WorkerKind, WorkerResult, WorkerRow, WorkerState
from tai42_skeleton.app.recycle import (
    FAILED,
    RECYCLED,
    SELF_DEFERRED,
    TIMED_OUT,
    ApplierEntry,
    RecycleError,
    RecycleReport,
    RecycleRow,
    RecycleStop,
    RecycleTimeoutError,
    orchestrate_recycle,
)

_NOW = "2026-01-01T00:00:00+00:00"
_TTL = 15.0
_FRESH_PTTL = int(_TTL * 1000)


def _row(
    name: str,
    kind: WorkerKind,
    *,
    generation: int = 1,
    state: WorkerState = WorkerState.ready,
    pttl_ms: int | None = _FRESH_PTTL,
) -> WorkerRow:
    return WorkerRow(
        name=name,
        kind=kind,
        pid=1,
        generation=generation,
        joined_at=_NOW,
        beat_at=_NOW,
        state=state,
        pttl_ms=pttl_ms,
    )


# -- census transforms a recycle publish applies (what a respawn would do) ------

Transform = Callable[[list[WorkerRow], str], list[WorkerRow]]


def _drop(rows: list[WorkerRow], target: str) -> list[WorkerRow]:
    return [r for r in rows if r.name != target]


def reuse_freed_slot(rows: list[WorkerRow], target: str) -> list[WorkerRow]:
    """The replacement REUSES the freed slot name at the next generation — the slot-name
    reuse the new-origin heuristic could never see."""
    old = next(r for r in rows if r.name == target)
    return [*_drop(rows, target), _row(target, old.kind, generation=old.generation + 1)]


def different_name_replacement(rows: list[WorkerRow], target: str) -> list[WorkerRow]:
    """The old life ends and a NEW life of the kind joins under a DIFFERENT name — the
    double-fault shape that must CONFIRM (old gone + fresh capacity), never abort."""
    old = next(r for r in rows if r.name == target)
    fresh = _row(f"{old.kind.value}-99", old.kind, generation=1)
    return [*_drop(rows, target), fresh]


def supersede_in_place(rows: list[WorkerRow], target: str) -> list[WorkerRow]:
    """A survivor re-mints on the SAME name at a higher generation (old life gone by a
    superseding generation, and it counts as fresh capacity)."""
    return reuse_freed_slot(rows, target)


def old_gone_no_capacity(rows: list[WorkerRow], target: str) -> list[WorkerRow]:
    """Old life gone, but NO new ready capacity joins — fresh-capacity fact unsatisfied."""
    return _drop(rows, target)


def capacity_but_old_stays(rows: list[WorkerRow], target: str) -> list[WorkerRow]:
    """Fresh capacity joins, but the target's old life NEVER leaves — old-life-gone
    unsatisfied."""
    old = next(r for r in rows if r.name == target)
    return [*rows, _row(f"{old.kind.value}-99", old.kind, generation=1)]


class _ScriptedBus:
    """Scripted census + publish. A successful recycle applies ``transform`` to the
    census (defaulting to slot-name reuse). The ``*_fault`` knobs drive loud paths.

    ``target_outcome`` is the verdict every recycle publish returns, with
    ``outcome_detail`` / ``outcome_error`` riding on it. ``transform_after_reads``
    models a target that takes the recycle op up late: instead of applying the
    transform at publish time, it is applied on that many-th census read after the
    publish, whatever the verdict was. ``census_raises`` makes the N-th census read raise
    the given exception (the reads before and after it answer normally)."""

    def __init__(
        self,
        rows: list[WorkerRow],
        *,
        transform: Transform = reuse_freed_slot,
        target_outcome: OpOutcome = OpOutcome.applied,
        outcome_detail: str | None = None,
        outcome_error: str | None = None,
        transform_after_reads: int | None = None,
        census_raises: tuple[int, Exception] | None = None,
        reachable: bool = True,
        empty_results: bool = False,
    ) -> None:
        self._rows = list(rows)
        self._transform = transform
        self._target_outcome = target_outcome
        self._outcome_detail = outcome_detail
        self._outcome_error = outcome_error
        self._transform_after_reads = transform_after_reads
        self._reachable = reachable
        self._empty_results = empty_results
        self._pending: tuple[str, int] | None = None
        self._census_raises = census_raises
        self._census_reads = 0
        self.published: list[tuple[str, tuple[str, ...]]] = []

    @property
    def heartbeat_ttl(self) -> float:
        return _TTL

    async def census(self) -> list[WorkerRow]:
        self._census_reads += 1
        if self._census_raises is not None and self._census_raises[0] == self._census_reads:
            raise self._census_raises[1]
        if self._pending is not None:
            target, remaining = self._pending
            remaining -= 1
            if remaining <= 0:
                self._rows = self._transform(list(self._rows), target)
                self._pending = None
            else:
                self._pending = (target, remaining)
        return list(self._rows)

    async def publish(self, op: dict[str, Any], targets: list[str] | None, local: Any) -> FleetResult:
        target = (targets or [None])[0]
        assert target is not None
        self.published.append((op["op"], tuple(targets or ())))
        if not self._reachable:
            return FleetResult(op=op["op"], reachable=False, error="bus unreachable")
        if self._empty_results:
            return FleetResult(op=op["op"], results=[])
        if self._transform_after_reads is not None:
            self._pending = (target, self._transform_after_reads)
        elif self._target_outcome is OpOutcome.applied and any(r.name == target for r in self._rows):
            self._rows = self._transform(list(self._rows), target)
        return FleetResult(
            op=op["op"],
            results=[
                WorkerResult(
                    name=target,
                    outcome=self._target_outcome,
                    detail=self._outcome_detail,
                    error=self._outcome_error,
                )
            ],
        )


def _bus(fake: _ScriptedBus) -> WorkerBus:
    return cast("WorkerBus", fake)


async def _run(fake: _ScriptedBus, *, excluded_name: str, kinds: list[WorkerKind], deferred: bool) -> RecycleReport:
    return await orchestrate_recycle(
        _bus(fake),
        excluded_name=excluded_name,
        applier_generation=7,
        target_kinds=kinds,
        applier_self_deferred=deferred,
        step_timeout=1.0,
        poll_interval=0.001,
    )


# -- happy path: rolling recycle with slot-name reuse + self-deferred applier ---


async def test_rolls_each_kind_and_records_the_report() -> None:
    fake = _ScriptedBus(
        [
            _row("backend-1", WorkerKind.backend),
            _row("backend-2", WorkerKind.backend),
            _row("serve-1", WorkerKind.serve),
        ]
    )
    report = await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend, WorkerKind.serve], deferred=True)

    # Both backend workers recycled one at a time; the only serve is the excluded applier.
    assert [(r.name, r.status) for r in report.rows] == [("backend-1", RECYCLED), ("backend-2", RECYCLED)]
    assert all(r.kind == "backend" for r in report.rows)
    assert [r.generation_before for r in report.rows] == [1, 1]
    # The fresh list is the new backend lives (slot reused at gen 2) — never a successor.
    assert sorted((f.name, f.generation) for f in report.fresh) == [("backend-1", 2), ("backend-2", 2)]
    # The applier's own recycle is deferred, carrying its own current generation.
    assert report.applier is not None
    assert report.applier.name == "serve-1"
    assert report.applier.generation == 7
    assert report.applier.status == SELF_DEFERRED
    # One recycle op per retired worker, each targeted to exactly that slot.
    assert fake.published == [("recycle", ("backend-1",)), ("recycle", ("backend-2",))]


async def test_backend_only_diff_produces_no_applier_entry() -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend), _row("serve-1", WorkerKind.serve)])
    report = await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    assert [r.name for r in report.rows] == ["backend-1"]
    assert report.applier is None


async def test_the_excluded_applier_is_never_targeted_even_within_its_kind() -> None:
    fake = _ScriptedBus([_row("serve-1", WorkerKind.serve), _row("serve-2", WorkerKind.serve)])
    report = await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.serve], deferred=True)
    assert [r.name for r in report.rows] == ["serve-2"]
    assert all(target != ("serve-1",) for _op, target in fake.published)


async def test_report_carries_no_generation_after_and_no_replacements() -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend), _row("serve-1", WorkerKind.serve)])
    report = await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=True)
    blob = report.model_dump_json()
    assert report.rows[0].detail is None
    assert "generation_after" not in blob
    assert "replacements" not in blob


# -- the two acceptance facts -------------------------------------------------


async def test_old_life_gone_by_superseding_generation_confirms() -> None:
    # The target's row stays under its NAME but at a higher generation — old life gone by
    # supersession, and that new life is the counted fresh capacity.
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], transform=supersede_in_place)
    report = await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    assert [(r.name, r.status) for r in report.rows] == [("backend-1", RECYCLED)]
    assert [(f.name, f.generation) for f in report.fresh] == [("backend-1", 2)]


async def test_double_fault_old_gone_and_fresh_under_a_different_name_confirms() -> None:
    # The old NAME is gone AND the fresh life joined under a DIFFERENT name — both facts
    # met, so convergence CONFIRMS (never aborts on the name mismatch).
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], transform=different_name_replacement)
    report = await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    assert [(r.name, r.status) for r in report.rows] == [("backend-1", RECYCLED)]
    assert [(f.name, f.generation) for f in report.fresh] == [("backend-99", 1)]


async def test_timeout_when_fresh_capacity_stays_short_names_that_fact() -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], transform=old_gone_no_capacity)
    with pytest.raises(RecycleTimeoutError) as excinfo:
        await orchestrate_recycle(
            _bus(fake),
            excluded_name="serve-1",
            applier_generation=1,
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=0.05,
            poll_interval=0.01,
        )
    err = excinfo.value
    assert err.name == "backend-1"
    assert "fresh READY capacity short" in err.unsatisfied
    assert "fresh READY capacity short" in str(err)
    stop_detail = err.report.rows[0].detail
    assert stop_detail is not None
    assert err.report.stopped == RecycleStop(kind="backend", name="backend-1", detail=stop_detail)
    # The partial report marks the target timed-out (its recycle applied, no convergence).
    assert isinstance(err.report, RecycleReport)
    assert [(r.name, r.status) for r in err.report.rows] == [("backend-1", TIMED_OUT)]


async def test_timeout_when_old_life_stays_present_names_that_fact() -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], transform=capacity_but_old_stays)
    with pytest.raises(RecycleTimeoutError) as excinfo:
        await orchestrate_recycle(
            _bus(fake),
            excluded_name="serve-1",
            applier_generation=1,
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=0.05,
            poll_interval=0.01,
        )
    assert excinfo.value.unsatisfied == "old life still present"
    assert [(r.name, r.status) for r in excinfo.value.report.rows] == [("backend-1", TIMED_OUT)]
    assert excinfo.value.report.stopped == RecycleStop(
        kind="backend", name="backend-1", detail="old life still present"
    )


# -- gap-row target: wait for ready before publishing --------------------------


class _GapThenReadyBus(_ScriptedBus):
    """A single backend target that starts ``resyncing`` and turns ``ready`` only after a
    few census reads — the gap-row ready-wait must hold the recycle op until then."""

    def __init__(self, ready_after: int) -> None:
        super().__init__([_row("backend-1", WorkerKind.backend, state=WorkerState.resyncing)])
        self._reads = 0
        self._ready_after = ready_after
        self._flipped = False

    async def census(self) -> list[WorkerRow]:
        self._reads += 1
        if not self._flipped and self._reads >= self._ready_after:
            # One-shot flip to ready; later publishes/transforms own the census after.
            self._rows = [_row("backend-1", WorkerKind.backend, state=WorkerState.ready)]
            self._flipped = True
        return list(self._rows)


async def test_gap_row_target_is_waited_to_ready_before_recycle() -> None:
    fake = _GapThenReadyBus(ready_after=3)
    report = await orchestrate_recycle(
        _bus(fake),
        excluded_name="serve-1",
        applier_generation=1,
        target_kinds=[WorkerKind.backend],
        applier_self_deferred=False,
        step_timeout=1.0,
        poll_interval=0.001,
    )
    # The recycle op was published only after the row turned ready, and it converged.
    assert fake.published == [("recycle", ("backend-1",))]
    assert [(r.name, r.status) for r in report.rows] == [("backend-1", RECYCLED)]


async def test_gap_row_that_never_readies_times_out_naming_its_state() -> None:
    # The target stays resyncing forever: the ready-wait budget lapses BEFORE any recycle
    # op is published, and the error names the slot and its last-seen state.
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend, state=WorkerState.resyncing)])
    with pytest.raises(RecycleTimeoutError) as excinfo:
        await orchestrate_recycle(
            _bus(fake),
            excluded_name="serve-1",
            applier_generation=1,
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=0.05,
            poll_interval=0.01,
        )
    assert "never returned to ready" in excinfo.value.unsatisfied
    assert "resyncing" in excinfo.value.unsatisfied
    assert fake.published == []  # no recycle op was ever sent
    assert [(r.name, r.status) for r in excinfo.value.report.rows] == [("backend-1", TIMED_OUT)]


# -- a computed bus verdict is awaited on reality -------------------------------

_SILENT = "no received-ack within the ack timeout while presence stayed live — worker missing (alive but silent)"

_COMPUTED = [
    pytest.param(OpOutcome.missing, _SILENT, id="missing"),
    pytest.param(OpOutcome.departed, "presence expired before a reply — worker departed", id="departed"),
    pytest.param(OpOutcome.timed_out, "acked but did not apply within the apply timeout", id="timed_out"),
]


@pytest.mark.parametrize(("outcome", "detail"), _COMPUTED)
async def test_a_target_the_bus_could_not_confirm_recycles_once_reality_follows(
    outcome: OpOutcome, detail: str
) -> None:
    # The target was still busy when the recycle op arrived: the bus computed a verdict
    # instead of hearing ``applied``. It takes the buffered op up later and its slot is
    # reused at the next generation — the acceptance facts hold, so the row is recycled.
    fake = _ScriptedBus(
        [_row("backend-1", WorkerKind.backend)],
        target_outcome=outcome,
        outcome_detail=detail,
        transform_after_reads=3,
    )
    report = await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    assert [(r.name, r.status) for r in report.rows] == [("backend-1", RECYCLED)]
    assert report.rows[0].detail is None
    assert report.converged is True
    assert fake.published == [("recycle", ("backend-1",))]


@pytest.mark.parametrize(("outcome", "detail"), _COMPUTED)
async def test_a_target_the_bus_could_not_confirm_that_never_converges_lands_as_a_timed_out_row(
    outcome: OpOutcome, detail: str
) -> None:
    fake = _ScriptedBus(
        [_row("backend-1", WorkerKind.backend), _row("backend-2", WorkerKind.backend)],
        target_outcome=outcome,
        outcome_detail=detail,
    )
    with pytest.raises(RecycleTimeoutError) as excinfo:
        await orchestrate_recycle(
            _bus(fake),
            excluded_name="serve-1",
            applier_generation=1,
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=True,
            step_timeout=0.05,
            poll_interval=0.01,
        )
    err = excinfo.value
    assert err.unsatisfied == "old life still present"
    assert [(r.name, r.status) for r in err.report.rows] == [("backend-1", TIMED_OUT)]
    row_detail = err.report.rows[0].detail
    assert row_detail is not None
    assert "old life still present" in row_detail
    assert detail in row_detail
    assert err.report.converged is False
    assert err.report.stopped == RecycleStop(kind="backend", name="backend-1", detail=row_detail)
    # The roll stopped at the row: the second target and the applier's own recycle are not attempted.
    assert fake.published == [("recycle", ("backend-1",))]
    assert err.report.applier is None


# -- definite failures stop the roll with their row -----------------------------


async def test_a_failed_recycle_terminal_stops_the_roll_with_its_row() -> None:
    fake = _ScriptedBus(
        [_row("backend-1", WorkerKind.backend), _row("backend-2", WorkerKind.backend)],
        target_outcome=OpOutcome.failed,
        outcome_error="RuntimeError: boom",
    )
    with pytest.raises(RecycleError) as excinfo:
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    report = excinfo.value.report
    assert [(r.name, r.status, r.detail) for r in report.rows] == [("backend-1", FAILED, "RuntimeError: boom")]
    assert report.stopped == RecycleStop(kind="backend", name="backend-1", detail="RuntimeError: boom")
    assert report.converged is False
    assert "backend-1" in str(excinfo.value)
    # Nothing was published to the second target of the kind.
    assert fake.published == [("recycle", ("backend-1",))]


async def test_a_reply_naming_no_target_raises() -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], empty_results=True)
    with pytest.raises(RecycleError, match="did not apply") as excinfo:
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    # A bus contract violation records no stop: the report reads converged and the caller re-raises it.
    assert excinfo.value.report.stopped is None
    assert excinfo.value.report.rows == []


async def test_an_unreachable_bus_stops_the_roll_with_its_row() -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], reachable=False)
    with pytest.raises(RecycleError, match="unreachable") as excinfo:
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    report = excinfo.value.report
    assert [(r.name, r.status, r.detail) for r in report.rows] == [("backend-1", FAILED, "bus unreachable")]
    assert report.stopped == RecycleStop(kind="backend", name="backend-1", detail="bus unreachable")
    assert report.converged is False


# -- the targets are pinned at the apply's start --------------------------------


class _AbsentThenBackBus(_ScriptedBus):
    """A pinned backend absent from the census (its slot lost, re-minting) for a few
    reads, then back ``resyncing`` at a NEW generation, then ``ready``; ``returns=False``
    keeps it absent for good."""

    def __init__(self, *, absent_reads: int, returns: bool = True) -> None:
        super().__init__([_row("serve-1", WorkerKind.serve)])
        self._reads = 0
        self._absent_reads = absent_reads
        self._returns = returns

    async def census(self) -> list[WorkerRow]:
        self._reads += 1
        if self._returns and not self.published:
            if self._reads == self._absent_reads + 1:
                self._rows = [
                    _row("serve-1", WorkerKind.serve),
                    _row("backend-1", WorkerKind.backend, generation=2, state=WorkerState.resyncing),
                ]
            elif self._reads == self._absent_reads + 2:
                self._rows = [_row("serve-1", WorkerKind.serve), _row("backend-1", WorkerKind.backend, generation=2)]
        return list(self._rows)


async def test_a_pinned_target_absent_at_roll_time_is_waited_for_and_recycled() -> None:
    fake = _AbsentThenBackBus(absent_reads=3)
    report = await orchestrate_recycle(
        _bus(fake),
        excluded_name="serve-1",
        applier_generation=1,
        target_kinds=[WorkerKind.backend],
        applier_self_deferred=False,
        step_timeout=1.0,
        poll_interval=0.001,
        expected={"backend-1": (WorkerKind.backend, 1)},
    )
    assert fake.published == [("recycle", ("backend-1",))]
    assert [(r.name, r.status, r.generation_before) for r in report.rows] == [("backend-1", RECYCLED, 2)]
    # The life it returned with is not fresh capacity; the life after its recycle is.
    assert [(f.name, f.generation) for f in report.fresh] == [("backend-1", 3)]


async def test_a_pinned_target_that_never_returns_lands_as_a_timed_out_row() -> None:
    fake = _AbsentThenBackBus(absent_reads=0, returns=False)
    with pytest.raises(RecycleTimeoutError) as excinfo:
        await orchestrate_recycle(
            _bus(fake),
            excluded_name="serve-1",
            applier_generation=1,
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=0.05,
            poll_interval=0.01,
            expected={"backend-1": (WorkerKind.backend, 1)},
        )
    report = excinfo.value.report
    assert [(r.name, r.status, r.generation_before) for r in report.rows] == [("backend-1", TIMED_OUT, 1)]
    assert report.rows[0].detail is not None
    assert "last state: absent" in report.rows[0].detail
    assert report.stopped == RecycleStop(kind="backend", name="backend-1", detail=report.rows[0].detail)
    assert fake.published == []


class _PinnedReturnsMidRollBus(_ScriptedBus):
    """``backend-2`` is on the census; the pinned ``backend-1`` is in its re-mint gap and
    returns ready at generation 2 after a few reads. ``backend-2``'s recycle ends its old
    life but brings no capacity; ``backend-1``'s recycle reuses its slot."""

    def __init__(self) -> None:
        super().__init__([_row("serve-1", WorkerKind.serve), _row("backend-2", WorkerKind.backend)])
        self._reads = 0

    async def census(self) -> list[WorkerRow]:
        self._reads += 1
        if self._reads == 3:
            self._rows = [*self._rows, _row("backend-1", WorkerKind.backend, generation=2)]
        return list(self._rows)

    async def publish(self, op: dict[str, Any], targets: list[str] | None, local: Any) -> FleetResult:
        target = (targets or [None])[0]
        assert target is not None
        self.published.append((op["op"], (target,)))
        transform = reuse_freed_slot if target == "backend-1" else old_gone_no_capacity
        self._rows = transform(list(self._rows), target)
        return FleetResult(op=op["op"], results=[WorkerResult(name=target, outcome=OpOutcome.applied)])


async def test_a_pinned_target_returning_mid_roll_is_not_counted_as_another_targets_capacity() -> None:
    # The life backend-1 returns with is the same unrecycled worker, never a replacement
    # for backend-2: backend-2's step must time out short of capacity.
    fake = _PinnedReturnsMidRollBus()
    with pytest.raises(RecycleTimeoutError) as excinfo:
        await orchestrate_recycle(
            _bus(fake),
            excluded_name="serve-1",
            applier_generation=1,
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=0.2,
            poll_interval=0.001,
            expected={"backend-1": (WorkerKind.backend, 1), "backend-2": (WorkerKind.backend, 1)},
        )
    err = excinfo.value
    assert err.name == "backend-2"
    assert "fresh READY capacity short" in err.unsatisfied
    assert {(r.name, r.status) for r in err.report.rows} == {("backend-1", RECYCLED), ("backend-2", TIMED_OUT)}


# -- the converged predicate ------------------------------------------------------


def test_converged_is_every_row_recycled_and_ignores_the_applier_entry() -> None:
    def report(*statuses: str) -> RecycleReport:
        return RecycleReport(
            rows=[
                RecycleRow(name=f"backend-{i}", kind="backend", generation_before=1, status=status)
                for i, status in enumerate(statuses)
            ],
            applier=ApplierEntry(name="serve-1", generation=1),
        )

    assert report().converged is True
    assert report(RECYCLED, RECYCLED).converged is True
    assert report(RECYCLED, TIMED_OUT).converged is False
    assert report(FAILED).converged is False
    stopped = report(RECYCLED)
    stopped.stopped = RecycleStop(kind="backend", name=None, detail="bus unreachable")
    assert stopped.converged is False
    assert RecycleReport().converged is True


# -- a bus outage on a census read is the roll's stop ------------------------------

_OUTAGES = [
    pytest.param(RedisConnectionError("Error 111 connecting to redis:6379. Connection refused."), id="connection"),
    pytest.param(RedisTimeoutError("Timeout reading from socket"), id="timeout"),
    pytest.param(ClientDisconnectedError("connection severed"), id="disconnected"),
]


@pytest.mark.parametrize("outage", _OUTAGES)
async def test_an_outage_on_the_kind_start_read_stops_the_roll_with_no_target_in_hand(outage: Exception) -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], census_raises=(1, outage))
    with pytest.raises(RecycleError) as excinfo:
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=True)
    report = excinfo.value.report
    assert excinfo.value.__cause__ is outage
    assert report.rows == []
    assert report.stopped is not None
    assert report.stopped.kind == "backend"
    assert report.stopped.name is None
    assert type(outage).__name__ in report.stopped.detail
    assert str(outage) in report.stopped.detail
    assert report.converged is False
    assert report.applier is None
    assert fake.published == []


async def test_an_outage_while_waiting_for_a_target_to_return_fails_its_row() -> None:
    outage = RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend, state=WorkerState.resyncing)], census_raises=(2, outage))
    with pytest.raises(RecycleError) as excinfo:
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    report = excinfo.value.report
    assert [(r.name, r.status, r.generation_before) for r in report.rows] == [("backend-1", FAILED, 1)]
    detail = report.rows[0].detail
    assert detail is not None
    assert "Error 111" in detail
    assert report.stopped == RecycleStop(kind="backend", name="backend-1", detail=detail)
    assert fake.published == []


@pytest.mark.parametrize(
    ("outcome", "said"),
    [
        pytest.param(OpOutcome.applied, "applied by the target", id="applied"),
        pytest.param(OpOutcome.missing, "alive but silent", id="missing"),
    ],
)
async def test_an_outage_while_awaiting_convergence_fails_the_row_and_says_what_the_bus_heard(
    outcome: OpOutcome, said: str
) -> None:
    outage = RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")
    fake = _ScriptedBus(
        [_row("backend-1", WorkerKind.backend)],
        target_outcome=outcome,
        outcome_detail=_SILENT if outcome is OpOutcome.missing else None,
        census_raises=(3, outage),
    )
    with pytest.raises(RecycleError) as excinfo:
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    report = excinfo.value.report
    assert [(r.name, r.status) for r in report.rows] == [("backend-1", FAILED)]
    detail = report.rows[0].detail
    assert detail is not None
    assert "Error 111" in detail
    assert said in detail
    assert report.stopped == RecycleStop(kind="backend", name="backend-1", detail=detail)
    assert fake.published == [("recycle", ("backend-1",))]


async def test_an_outage_on_the_fresh_read_keeps_the_recycled_rows_and_stops_the_roll() -> None:
    outage = RedisConnectionError("Error 111 connecting to redis:6379. Connection refused.")
    fake = _ScriptedBus(
        [_row("backend-1", WorkerKind.backend), _row("serve-1", WorkerKind.serve), _row("serve-2", WorkerKind.serve)],
        census_raises=(4, outage),
    )
    with pytest.raises(RecycleError) as excinfo:
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend, WorkerKind.serve], deferred=True)
    report = excinfo.value.report
    assert [(r.name, r.status, r.detail) for r in report.rows] == [("backend-1", RECYCLED, None)]
    assert report.stopped is not None
    assert report.stopped.kind == "backend"
    assert report.stopped.name is None
    assert "fresh" in report.stopped.detail
    assert "Error 111" in report.stopped.detail
    assert report.fresh == []
    assert report.converged is False
    assert report.applier is None
    # The serve roll is not attempted.
    assert fake.published == [("recycle", ("backend-1",))]


async def test_a_bus_fault_that_is_not_an_outage_passes_through() -> None:
    fault = RedisResponseError("WRONGTYPE Operation against a key holding the wrong kind of value")
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], census_raises=(1, fault))
    with pytest.raises(RedisResponseError) as excinfo:
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    assert excinfo.value is fault
