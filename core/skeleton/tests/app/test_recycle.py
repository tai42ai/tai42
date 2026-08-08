"""Recycle orchestration — the rolling per-kind loop.

Driven against a scripted fake bus so the census/publish sequence is deterministic:
each recycle publish retires the target and (on the success path) joins a genuinely
NEW same-kind origin, exactly as a supervised respawn + boot resync would. The tests
pin the aggregate report shape (recycled / replacements / timeouts / self-deferred
applier), the loud timeout, and the names-only invariant.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest

from tai42_skeleton.app.bus import FleetResult, OpOutcome, WorkerBus, WorkerKind, WorkerResult, WorkerRow, WorkerState
from tai42_skeleton.app.recycle import (
    SELF_DEFERRED,
    RecycleError,
    RecycleReport,
    RecycleTimeoutError,
    orchestrate_recycle,
)

_NOW = "2026-01-01T00:00:00+00:00"


def _worker(name: str, kind: WorkerKind) -> WorkerRow:
    return WorkerRow(name=name, kind=kind, pid=1, generation=1, joined_at=_NOW, beat_at=_NOW, state=WorkerState.ready)


def _fresh_name(kind: WorkerKind) -> str:
    return f"{kind.value}-{uuid.uuid4().hex}"


class _FakeBus:
    """Scripted census + publish. A successful recycle retires the target and joins a
    fresh same-kind worker; the ``*_fault`` knobs drive the loud-failure paths."""

    def __init__(
        self,
        origins: list[WorkerRow],
        *,
        join_replacement: bool = True,
        target_outcome: OpOutcome = OpOutcome.applied,
        reachable: bool = True,
        empty_results: bool = False,
    ) -> None:
        self._origins = list(origins)
        self.published: list[tuple[str, tuple[str, ...]]] = []
        self._join_replacement = join_replacement
        self._target_outcome = target_outcome
        self._reachable = reachable
        self._empty_results = empty_results

    async def census(self) -> list[WorkerRow]:
        return list(self._origins)

    async def publish(self, op: dict[str, Any], targets: list[str] | None, local: Any) -> FleetResult:
        target = (targets or [None])[0]
        assert target is not None
        self.published.append((op["op"], tuple(targets or ())))
        if not self._reachable:
            return FleetResult(op=op["op"], reachable=False, error="bus unreachable")
        if self._empty_results:
            return FleetResult(op=op["op"], results=[])
        old = next((o for o in self._origins if o.name == target), None)
        if self._join_replacement and old is not None and self._target_outcome is OpOutcome.applied:
            self._origins = [o for o in self._origins if o.name != target]
            self._origins.append(_worker(_fresh_name(old.kind), old.kind))
        return FleetResult(op=op["op"], results=[WorkerResult(name=target, outcome=self._target_outcome)])


def _bus(fake: _FakeBus) -> WorkerBus:
    return cast("WorkerBus", fake)


def _origins(**counts: int) -> dict[str, WorkerRow]:
    made: dict[str, WorkerRow] = {}
    for kind_name, n in counts.items():
        kind = WorkerKind(kind_name)
        for i in range(n):
            made[f"{kind_name}{i}"] = _worker(_fresh_name(kind), kind)
    return made


# -- happy path: rolling recycle with a self-deferred applier ------------------


async def test_rolls_each_kind_and_records_the_report() -> None:
    o = _origins(backend=2, serve=1)
    applier = o["serve0"].name
    fake = _FakeBus(list(o.values()))

    report = await orchestrate_recycle(
        _bus(fake),
        excluded_origin=applier,
        target_kinds=[WorkerKind.backend, WorkerKind.serve],
        applier_self_deferred=True,
        step_timeout=1.0,
        poll_interval=0.001,
    )

    # Both backend workers recycled one at a time; the only serve origin is the
    # excluded applier, so serve rolls nobody.
    assert report.recycled == [o["backend0"].name, o["backend1"].name]
    assert len(report.replacements) == 2
    assert all(name.startswith("backend-") for name in report.replacements)
    assert report.timeouts == []
    # The applier's own recycle is deferred, reported as self-deferred, never recycled.
    assert report.applier is not None
    assert report.applier.origin == applier
    assert report.applier.status == SELF_DEFERRED
    assert applier not in report.recycled
    # One recycle op per retired worker, each targeted to exactly that origin.
    assert fake.published == [
        ("recycle", (o["backend0"].name,)),
        ("recycle", (o["backend1"].name,)),
    ]


async def test_backend_only_diff_produces_no_applier_entry() -> None:
    o = _origins(backend=1, serve=1)
    fake = _FakeBus(list(o.values()))

    report = await orchestrate_recycle(
        _bus(fake),
        excluded_origin=o["serve0"].name,
        target_kinds=[WorkerKind.backend],
        applier_self_deferred=False,
        step_timeout=1.0,
        poll_interval=0.001,
    )

    assert report.recycled == [o["backend0"].name]
    assert len(report.replacements) == 1
    assert report.applier is None


async def test_the_excluded_applier_is_never_targeted_even_within_its_kind() -> None:
    o = _origins(serve=2)
    applier = o["serve0"].name
    fake = _FakeBus(list(o.values()))

    report = await orchestrate_recycle(
        _bus(fake),
        excluded_origin=applier,
        target_kinds=[WorkerKind.serve],
        applier_self_deferred=True,
        step_timeout=1.0,
        poll_interval=0.001,
    )

    assert report.recycled == [o["serve1"].name]
    assert applier not in report.recycled
    assert all(target != (applier,) for _op, target in fake.published)


async def test_report_carries_names_only() -> None:
    o = _origins(backend=1, serve=1)
    fake = _FakeBus(list(o.values()))
    report = await orchestrate_recycle(
        _bus(fake),
        excluded_origin=o["serve0"].name,
        target_kinds=[WorkerKind.backend],
        applier_self_deferred=True,
        step_timeout=1.0,
        poll_interval=0.001,
    )
    names = [*report.recycled, *report.replacements, *report.timeouts]
    assert report.applier is not None
    names.append(report.applier.origin)
    # Every reported string is an origin identity ({kind}-{uuid}); no env value leaks.
    assert names
    assert all(name.startswith(("backend-", "serve-")) for name in names)


# -- loud failures ------------------------------------------------------------


async def test_a_replacement_that_never_joins_raises_and_names_the_origin() -> None:
    o = _origins(backend=1)
    fake = _FakeBus(list(o.values()), join_replacement=False)

    with pytest.raises(RecycleTimeoutError) as excinfo:
        await orchestrate_recycle(
            _bus(fake),
            excluded_origin="serve-applier",
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=0.05,
            poll_interval=0.01,
        )

    err = excinfo.value
    assert err.origin == o["backend0"].name
    assert o["backend0"].name in str(err)
    # The partial report rides the error: the worker was recycled, then timed out.
    assert isinstance(err.report, RecycleReport)
    assert err.report.recycled == [o["backend0"].name]
    assert err.report.timeouts == [o["backend0"].name]


async def test_a_recycle_op_that_does_not_apply_raises() -> None:
    o = _origins(backend=1)
    fake = _FakeBus(list(o.values()), target_outcome=OpOutcome.failed)

    with pytest.raises(RecycleError) as excinfo:
        await orchestrate_recycle(
            _bus(fake),
            excluded_origin="serve-applier",
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=1.0,
            poll_interval=0.01,
        )
    # Raised BEFORE recording the origin as recycled (the op never applied).
    assert excinfo.value.report.recycled == []
    assert o["backend0"].name in str(excinfo.value)


async def test_a_reply_naming_no_target_origin_raises() -> None:
    o = _origins(backend=1)
    fake = _FakeBus(list(o.values()), empty_results=True)

    with pytest.raises(RecycleError, match="did not apply"):
        await orchestrate_recycle(
            _bus(fake),
            excluded_origin="serve-applier",
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=1.0,
            poll_interval=0.01,
        )


async def test_an_unreachable_bus_raises() -> None:
    o = _origins(backend=1)
    fake = _FakeBus(list(o.values()), reachable=False)

    with pytest.raises(RecycleError, match="unreachable"):
        await orchestrate_recycle(
            _bus(fake),
            excluded_origin="serve-applier",
            target_kinds=[WorkerKind.backend],
            applier_self_deferred=False,
            step_timeout=1.0,
            poll_interval=0.01,
        )
