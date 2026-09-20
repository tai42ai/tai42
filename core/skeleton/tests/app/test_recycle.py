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

from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerBus, WorkerKind, WorkerResult, WorkerRow, WorkerState
from tai42_skeleton.app.recycle import (
    RECYCLED,
    SELF_DEFERRED,
    TIMED_OUT,
    RecycleError,
    RecycleReport,
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
    census (defaulting to slot-name reuse). The ``*_fault`` knobs drive loud paths."""

    def __init__(
        self,
        rows: list[WorkerRow],
        *,
        transform: Transform = reuse_freed_slot,
        target_outcome: OpOutcome = OpOutcome.applied,
        reachable: bool = True,
        empty_results: bool = False,
    ) -> None:
        self._rows = list(rows)
        self._transform = transform
        self._target_outcome = target_outcome
        self._reachable = reachable
        self._empty_results = empty_results
        self.published: list[tuple[str, tuple[str, ...]]] = []

    @property
    def heartbeat_ttl(self) -> float:
        return _TTL

    async def census(self) -> list[WorkerRow]:
        return list(self._rows)

    async def publish(self, op: dict[str, Any], targets: list[str] | None, local: Any) -> FleetResult:
        target = (targets or [None])[0]
        assert target is not None
        self.published.append((op["op"], tuple(targets or ())))
        if not self._reachable:
            return FleetResult(op=op["op"], reachable=False, error="bus unreachable")
        if self._empty_results:
            return FleetResult(op=op["op"], results=[])
        if self._target_outcome is OpOutcome.applied and any(r.name == target for r in self._rows):
            self._rows = self._transform(list(self._rows), target)
        return FleetResult(op=op["op"], results=[WorkerResult(name=target, outcome=self._target_outcome)])


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


# -- loud failures ------------------------------------------------------------


async def test_a_recycle_op_that_does_not_apply_raises() -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], target_outcome=OpOutcome.failed)
    with pytest.raises(RecycleError) as excinfo:
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
    # Raised BEFORE recording the target as recycled (the op never applied).
    assert excinfo.value.report.rows == []
    assert "backend-1" in str(excinfo.value)


async def test_a_reply_naming_no_target_raises() -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], empty_results=True)
    with pytest.raises(RecycleError, match="did not apply"):
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)


async def test_an_unreachable_bus_raises() -> None:
    fake = _ScriptedBus([_row("backend-1", WorkerKind.backend)], reachable=False)
    with pytest.raises(RecycleError, match="unreachable"):
        await _run(fake, excluded_name="serve-1", kinds=[WorkerKind.backend], deferred=False)
