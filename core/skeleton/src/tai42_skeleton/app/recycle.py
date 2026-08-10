"""Recycle orchestration — the rolling per-kind recycle loop.

Called from the profile-apply pipeline when the diff carries recycle-class keys on a
supervised shape. It recycles the fleet ONE worker at a time per kind, and confirms
each step on REALITY rather than on a successor's identity: after publishing the
recycle op to a target it awaits two facts — the target's OLD life is gone, and enough
NEW READY capacity of that kind has joined to cover the recycled targets so far. Rolling
one kind at a time keeps the fleet from losing every worker of a kind at once.

Each target is recorded as ``(name, generation)`` against a per-kind pre-apply snapshot,
so a replacement that takes over the freed slot name (at a higher generation) is
recognised as a NEW life, not the same worker. The report names each recycled target
with its ``generation_before`` and a per-kind ``fresh`` list of the new lives observed
since the snapshot — informational, never claimed as any target's successor and never
carrying a post-recycle generation.

The applying worker is EXCLUDED by name: the bus echo-skips the publisher's own frames,
so the applier can never handle its own recycle op. When the diff carries serve-affecting
recycle keys the applier's OWN recycle is a deferred post-response self-exit it cannot
confirm, reported as an ``applier`` entry with the :data:`SELF_DEFERRED` status.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from pydantic import BaseModel, Field

from tai42_skeleton.app.bus import OpOutcome, WorkerBus, WorkerKind, WorkerRow, WorkerState, presence_fresh

RECYCLED = "recycled"
"""A target whose recycle op applied and whose convergence was confirmed."""

TIMED_OUT = "timed-out"
"""A target whose convergence (old life gone + fresh capacity) was not confirmed within
the step budget."""

SELF_DEFERRED = "self-deferred"
"""The applier self-entry status in a recycle report: the applying serve worker's own
recycle is a deferred post-response self-exit it cannot confirm. One shared literal, so
the applier self-entry status never diverges across its readers."""

# Recycle one KIND at a time in this order: a backend replacement must be serving before
# serve workers roll, so no kind loses every worker at once.
_KIND_ORDER: tuple[WorkerKind, ...] = (WorkerKind.backend, WorkerKind.serve)


class RecycleRow(BaseModel):
    """One recycled target's outcome: its pre-apply identity and terminal status
    (:data:`RECYCLED` once convergence confirmed, :data:`TIMED_OUT` on the step budget).
    ``generation_before`` is the life that was recycled — never a post-recycle life."""

    name: str
    kind: str
    generation_before: int
    status: str


class FreshLife(BaseModel):
    """A NEW READY life of a kind observed on the census since the pre-apply snapshot —
    a name absent from the snapshot, or a higher generation on a snapshot name. Surfaced
    as evidence of fresh capacity, never claimed as any target's successor."""

    name: str
    kind: str
    generation: int


class ApplierEntry(BaseModel):
    """The applier's own deferred self-exit entry — present only when the diff carries
    serve-affecting recycle keys. Carries the applier's own name and current generation;
    it is unconfirmed by design (the applier cannot recycle itself in-band)."""

    name: str
    generation: int
    status: str = SELF_DEFERRED


class RecycleReport(BaseModel):
    """The aggregate outcome of a recycle orchestration. ``rows`` is one entry per
    recycled target (its ``generation_before`` and terminal status); ``fresh`` is the new
    READY lives observed since the pre-apply snapshot, per kind — evidence of capacity,
    never a claimed successor; ``applier`` (when set) is the applier's deferred
    self-exit."""

    rows: list[RecycleRow] = Field(default_factory=list)
    fresh: list[FreshLife] = Field(default_factory=list)
    applier: ApplierEntry | None = None


class RecycleError(RuntimeError):
    """A recycle step failed loudly; carries the partial :class:`RecycleReport` so the
    caller can surface what converged before the failure."""

    def __init__(self, message: str, report: RecycleReport) -> None:
        super().__init__(message)
        self.report = report


class RecycleTimeoutError(RecycleError):
    """A recycled target's convergence was not confirmed within the step budget. Names
    the target and which fact stayed unsatisfied (old life still present, or fresh READY
    capacity short); the whole apply aborts."""

    def __init__(self, name: str, unsatisfied: str, report: RecycleReport) -> None:
        super().__init__(
            f"recycle: convergence for {name!r} was not confirmed within the step budget ({unsatisfied})",
            report,
        )
        self.name = name
        self.unsatisfied = unsatisfied


async def orchestrate_recycle(
    bus: WorkerBus,
    *,
    excluded_name: str,
    applier_generation: int,
    target_kinds: Sequence[WorkerKind],
    applier_self_deferred: bool,
    step_timeout: float,
    poll_interval: float = 0.2,
) -> RecycleReport:
    """Roll a recycle across the fleet, kind by kind, excluding ``excluded_name``.

    For each targeted kind, snapshot the live lives (``name -> generation``) and the
    targets (``(name, generation)``, applier excluded), then recycle them one at a time:
    a gap-row target is first waited to ``ready`` (its own per-phase budget), the recycle
    op is published to the single target, and convergence is awaited — the target's old
    life gone AND enough new READY capacity of the kind to cover the recycled targets so
    far. A recycle op that does not apply, or a convergence that is not confirmed within
    ``step_timeout``, raises loudly (:class:`RecycleError` / :class:`RecycleTimeoutError`)
    carrying the partial report.
    """
    report = RecycleReport()
    if applier_self_deferred:
        report.applier = ApplierEntry(name=excluded_name, generation=applier_generation)

    ttl = bus.heartbeat_ttl
    wanted = set(target_kinds)
    for kind in _KIND_ORDER:
        if kind not in wanted:
            continue
        # ONE census read backs both the snapshot and the targets: a worker joining
        # between two reads would be a target absent from the snapshot and inflate the
        # fresh-capacity count into a premature false convergence.
        rows = [row for row in await bus.census() if row.kind is kind]
        # Per-kind pre-apply snapshot: every live life of this kind, name -> generation.
        snapshot = {row.name: row.generation for row in rows}
        # Targets = every row of this kind except the applier, captured as (name,
        # generation) so a replacement taking over the freed name is a distinct life.
        targets = [(row.name, row.generation) for row in rows if row.name != excluded_name]
        for index, (name, generation) in enumerate(targets):
            # A gap-row target (not ready+fresh) is first waited to ready on its OWN
            # per-phase budget, so a slow ready-wait never starves the convergence await.
            # The generation observed when it becomes ready is the life actually recycled.
            recycled_generation = await _await_ready(
                bus, name, generation, kind, ttl, step_timeout, poll_interval, report
            )
            await _recycle_one(bus, name, recycled_generation, kind, report)
            # Recycled-so-far count (this target included) the convergence await must see
            # covered by NEW ready capacity.
            recycled = index + 1
            await _await_convergence(
                bus, kind, snapshot, name, recycled_generation, recycled, ttl, step_timeout, poll_interval, report
            )
        # The new ready+fresh lives of this kind since the snapshot — informational capacity.
        report.fresh.extend(_new_ready_lives(await bus.census(), kind, snapshot, ttl))
    return report


async def _await_ready(
    bus: WorkerBus,
    name: str,
    generation: int,
    kind: WorkerKind,
    ttl: float,
    step_timeout: float,
    poll_interval: float,
    report: RecycleReport,
) -> int:
    """Wait for a gap-row target to return ``state=ready`` with a fresh beat before the
    recycle op is published, returning the generation observed when it becomes ready — the
    life the recycle op will actually target (a gap row that re-mints during the wait is
    recycled at its NEW generation). A target already ready+fresh returns its current
    generation at once. This ready-wait gets its OWN per-phase budget (reset here); if it
    lapses, a loud :class:`RecycleTimeoutError` names the slot and its last-seen state."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + step_timeout
    while True:
        row = _row(await bus.census(), name)
        if row is not None and row.state == WorkerState.ready and presence_fresh(row.pttl_ms, ttl):
            return row.generation
        if loop.time() >= deadline:
            last = row.state.value if row is not None else "absent"
            report.rows.append(RecycleRow(name=name, kind=kind.value, generation_before=generation, status=TIMED_OUT))
            raise RecycleTimeoutError(name, f"gap-row target never returned to ready (last state: {last})", report)
        await asyncio.sleep(poll_interval)


async def _recycle_one(bus: WorkerBus, name: str, generation: int, kind: WorkerKind, report: RecycleReport) -> None:
    """Publish the recycle op to a single target and require its ``applied`` terminal,
    then record the recycled row. A bus-unreachable publish or a non-applied outcome
    raises loudly before any row is recorded."""
    result = await bus.publish({"op": "recycle"}, targets=[name], local=None)
    if not result.reachable:
        raise RecycleError(f"recycle: bus unreachable while recycling {name!r}: {result.error}", report)
    entry = next((r for r in result.results if r.name == name), None)
    if entry is None or entry.outcome is not OpOutcome.applied:
        detail = (entry.error or entry.detail) if entry is not None else "no reply from the target"
        raise RecycleError(f"recycle: worker {name!r} did not apply the recycle op ({detail})", report)
    report.rows.append(RecycleRow(name=name, kind=kind.value, generation_before=generation, status=RECYCLED))


async def _await_convergence(
    bus: WorkerBus,
    kind: WorkerKind,
    snapshot: dict[str, int],
    name: str,
    generation: int,
    recycled: int,
    ttl: float,
    step_timeout: float,
    poll_interval: float,
    report: RecycleReport,
) -> None:
    """Await the two acceptance facts within a fresh per-step deadline:

    - **old life gone** for ``(name, generation)``: presence[name] is absent/expired OR
      shows a generation greater than the target's (its row vanishing after
      ``state=recycling`` is the absent case);
    - **counted ready+fresh capacity**: ready rows of ``kind`` past the freshness gate on
      the census now whose life is NEW vs the snapshot, at least ``recycled`` of them
      (covering the targets recycled so far). This is a census-now fact, not a biography —
      a survivor that re-mints past its claim TTL mid-roll can inflate the count, a
      documented window.

    On the deadline the recycled row is marked :data:`TIMED_OUT` and a loud
    :class:`RecycleTimeoutError` names which fact stayed unsatisfied."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + step_timeout
    while True:
        rows = await bus.census()
        old_gone = _old_life_gone(rows, name, generation)
        fresh = len(_new_ready_lives(rows, kind, snapshot, ttl))
        if old_gone and fresh >= recycled:
            return
        if loop.time() >= deadline:
            if not old_gone:
                unsatisfied = "old life still present"
            else:
                unsatisfied = f"fresh READY capacity short ({fresh}/{recycled})"
            _mark_timed_out(report, name)
            raise RecycleTimeoutError(name, unsatisfied, report)
        await asyncio.sleep(poll_interval)


def _old_life_gone(rows: list[WorkerRow], name: str, generation: int) -> bool:
    """The target's old life is gone when its presence row is absent/expired, or a
    generation greater than the target's holds the name (a replacement took the slot)."""
    row = _row(rows, name)
    return row is None or row.generation > generation


def _new_ready_lives(rows: list[WorkerRow], kind: WorkerKind, snapshot: dict[str, int], ttl: float) -> list[FreshLife]:
    """The ready+fresh rows of ``kind`` whose life is NEW vs the snapshot — a name absent
    from it, or a generation greater than that name's snapshot generation. A ready-but-
    decayed row fails the freshness gate and is not counted as capacity, matching the
    ready+fresh discipline every other consumer applies."""
    return [
        FreshLife(name=row.name, kind=row.kind.value, generation=row.generation)
        for row in rows
        if row.kind is kind
        and row.state == WorkerState.ready
        and presence_fresh(row.pttl_ms, ttl)
        and (row.name not in snapshot or row.generation > snapshot[row.name])
    ]


def _mark_timed_out(report: RecycleReport, name: str) -> None:
    """Flip the recorded recycled row for ``name`` to :data:`TIMED_OUT` (its recycle op
    applied, but convergence was not confirmed)."""
    for row in report.rows:
        if row.name == name:
            row.status = TIMED_OUT
            return


def _row(rows: list[WorkerRow], name: str) -> WorkerRow | None:
    return next((row for row in rows if row.name == name), None)
