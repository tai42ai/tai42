"""Recycle orchestration — the rolling per-kind recycle loop.

Called from the profile-apply pipeline when the diff carries recycle-class keys on a
supervised shape. It recycles the fleet ONE worker at a time per kind, and confirms
each step on REALITY rather than on a successor's identity: after publishing the
recycle op to a target it awaits two facts — the target's OLD life is gone, and enough
NEW READY capacity of that kind has joined to cover the recycled targets so far. Rolling
one kind at a time keeps the fleet from losing every worker of a kind at once.

A worker's WIRE terminal is an answer: ``applied`` is confirmed on reality, ``failed``
records the target :data:`FAILED` with the worker's error. A verdict the publisher
COMPUTED from presence (``missing`` / ``departed`` / ``timed_out``, or a gap outcome) is
not an answer — the target may still take the op up once it is free — so the target is
awaited on the same acceptance facts within the step budget. The roll stops at the first
target that is not :data:`RECYCLED`, or when the bus cannot be read (an outage on any of
the roll's census reads, with or without a target in hand); later targets and the
applier's own recycle are not attempted, and the report's ``stopped`` record names where
and why.

The targets of a kind are the workers pinned at the apply's start unioned with the
census at roll time, each recorded as ``(name, generation)`` against a per-kind
snapshot, so a replacement that takes over the freed slot name (at a higher
generation) is recognised as a NEW life, not the same worker. A pinned worker absent
at roll time (its slot lost and re-minting) is waited to ready and recycled at the
generation it returns with. The report names each target with its
``generation_before``, its status and a ``detail`` for a target that did not
converge, and a per-kind ``fresh`` list of the new lives observed since the snapshot —
informational, never claimed as any target's successor and never carrying a
post-recycle generation.

A target costs at most the ready-wait budget, the bus apply timeout and the step
budget; nothing waits unbounded.

The applying worker is EXCLUDED by name: the bus echo-skips the publisher's own frames,
so the applier can never handle its own recycle op. When the diff carries serve-affecting
recycle keys the applier's OWN recycle is a deferred post-response self-exit it cannot
confirm, reported as an ``applier`` entry with the :data:`SELF_DEFERRED` status once
every target converged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence

from pydantic import BaseModel, Field

from tai42_skeleton.app.bus import (
    TRANSPORT_ERRORS,
    OpOutcome,
    WorkerBus,
    WorkerKind,
    WorkerResult,
    WorkerRow,
    WorkerState,
    presence_fresh,
)

logger = logging.getLogger(__name__)

RECYCLED = "recycled"
"""A target whose recycle op applied and whose convergence was confirmed."""

TIMED_OUT = "timed-out"
"""A target whose convergence (old life gone + fresh capacity) was not confirmed within
the step budget, or that never returned to ready."""

FAILED = "failed"
"""A target the roll could not carry through: its worker reported the recycle op
failed, or the bus could not be reached while the target was in hand (the op not
delivered, or the census not read)."""

SELF_DEFERRED = "self-deferred"
"""The applier self-entry status in a recycle report: the applying serve worker's own
recycle is a deferred post-response self-exit it cannot confirm. One shared literal, so
the applier self-entry status never diverges across its readers."""

# Recycle one KIND at a time in this order: a backend replacement must be serving before
# serve workers roll, so no kind loses every worker at once.
_KIND_ORDER: tuple[WorkerKind, ...] = (WorkerKind.backend, WorkerKind.serve)


class RecycleRow(BaseModel):
    """One target's outcome: its pre-recycle identity and terminal status.

    The status is :data:`RECYCLED` once convergence is confirmed, :data:`TIMED_OUT`
    on the step budget, or :data:`FAILED` when the worker reported the op failed or
    the bus could not deliver it. ``generation_before`` is the life that was
    recycled — never a post-recycle life. ``detail`` names why a target did not
    converge (the unsatisfied fact, plus the bus verdict when the target never
    confirmed the op; the worker's error; the transport error) and is ``None`` for
    a recycled target.
    """

    name: str
    kind: str
    generation_before: int
    status: str
    detail: str | None = None


class FreshLife(BaseModel):
    """A NEW READY life of a kind observed on the census since the pre-apply snapshot.

    A name absent from the snapshot, or a higher generation on a snapshot name.
    Surfaced as evidence of fresh capacity, never claimed as any target's
    successor.
    """

    name: str
    kind: str
    generation: int


class ApplierEntry(BaseModel):
    """The applier's own deferred self-exit entry, present only when the diff carries serve-affecting recycle keys.

    Carries the applier's own name and current generation; it is unconfirmed by
    design (the applier cannot recycle itself in-band).
    """

    name: str
    generation: int
    status: str = SELF_DEFERRED


class RecycleStop(BaseModel):
    """Where the roll stopped and why.

    ``name`` is the target the roll stopped at, or ``None`` when the bus could not be
    read with no target in hand (at a kind's start, or on its fresh-capacity read).
    """

    kind: str
    name: str | None
    detail: str


class RecycleReport(BaseModel):
    """The aggregate outcome of a recycle orchestration.

    ``rows`` is one entry per target the roll reached (its ``generation_before``,
    terminal status and detail), ending at the first one that did not converge;
    ``fresh`` is the new READY lives observed since the pre-apply snapshot, per
    kind — evidence of capacity, never a claimed successor; ``applier`` (when set)
    is the applier's deferred self-exit, present only on a converged roll.
    """

    rows: list[RecycleRow] = Field(default_factory=list)
    fresh: list[FreshLife] = Field(default_factory=list)
    applier: ApplierEntry | None = None
    stopped: RecycleStop | None = None

    @property
    def converged(self) -> bool:
        """Whether the roll ran to its end: no stop recorded and every target row :data:`RECYCLED`.

        The applier entry does not count.
        """
        return self.stopped is None and all(row.status == RECYCLED for row in self.rows)


class RecycleError(RuntimeError):
    """The roll stopped, carrying the partial :class:`RecycleReport`.

    The report's ``stopped`` record names where the roll stopped and why; its last
    row is that target when one was in hand, so the caller can surface what
    converged before it. A raise whose report carries no ``stopped`` record (the bus
    replied without naming the published target) is a bus contract violation.
    """

    def __init__(self, message: str, report: RecycleReport) -> None:
        """Build the error with ``message`` and attach the partial ``report``."""
        super().__init__(message)
        self.report = report


class RecycleTimeoutError(RecycleError):
    """A recycled target's convergence was not confirmed within the step budget.

    Names the target and which fact stayed unsatisfied (old life still present,
    fresh READY capacity short, or a target that never returned to ready); the roll
    stops at it.
    """

    def __init__(self, name: str, unsatisfied: str, report: RecycleReport) -> None:
        """Build the error naming ``name`` and the ``unsatisfied`` fact, attaching the partial ``report``."""
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
    expected: Mapping[str, tuple[WorkerKind, int]] | None = None,
    poll_interval: float = 0.2,
) -> RecycleReport:
    """Roll a recycle across the fleet, kind by kind, excluding ``excluded_name``.

    ``expected`` is the membership pinned at the apply's start, ``name -> (kind,
    generation)``, applier excluded. For each targeted kind, snapshot the live lives
    (``name -> generation``) and take as targets the pinned names of the kind unioned
    with the census rows of the kind (applier excluded); then recycle them one at a
    time: a target not ready+fresh (a gap row, or a pinned worker absent from the
    census) is first waited to ``ready`` on its own budget, the recycle op is published
    to the single target, and convergence is awaited — the target's old life gone AND
    enough new READY capacity of the kind to cover the recycled targets so far. A
    ``failed`` terminal or an undeliverable op records the target :data:`FAILED` and
    raises :class:`RecycleError`; a convergence or ready-wait not confirmed within
    ``step_timeout`` records it :data:`TIMED_OUT` and raises
    :class:`RecycleTimeoutError`. Every census read goes through :func:`_census`: a bus
    outage on one stops the roll — the target in hand, if any, recorded :data:`FAILED`
    — and raises :class:`RecycleError`. Each carries the partial report with its
    ``stopped`` record. The applier entry is set only once every target converged.
    """
    report = RecycleReport()
    pinned = expected or {}
    ttl = bus.heartbeat_ttl
    wanted = set(target_kinds)
    for kind in _KIND_ORDER:
        if kind not in wanted:
            continue
        # ONE census read backs both the snapshot and the roll-time targets: a worker
        # joining between two reads would be a target absent from the snapshot and
        # inflate the fresh-capacity count into a premature false convergence.
        rows = [
            row
            for row in await _census(
                bus, report, kind=kind, target=None, phase=f"reading the census at the start of the {kind.value} roll"
            )
            if row.kind is kind
        ]
        # Per-kind pre-apply snapshot: every live life of this kind, name -> generation.
        snapshot = {row.name: row.generation for row in rows}
        # Pinned names absent from the census go FIRST: each is waited back to ready and
        # its returned life entered into the snapshot before any other target's
        # convergence is counted, so that life — the same unrecycled worker — never
        # passes for another target's fresh capacity.
        absent = [
            (name, generation)
            for name, (pinned_kind, generation) in pinned.items()
            if pinned_kind is kind and name != excluded_name and name not in snapshot
        ]
        # Roll-time targets: every row of this kind except the applier, captured as
        # (name, generation) so a replacement taking over the freed name is a distinct life.
        present = [(row.name, row.generation) for row in rows if row.name != excluded_name]
        for index, (name, generation) in enumerate([*absent, *present]):
            # A target not ready+fresh is first waited to ready on its OWN per-phase
            # budget, so a slow ready-wait never starves the convergence await. The
            # generation observed when it becomes ready is the life actually recycled.
            recycled_generation = await _await_ready(
                bus, name, generation, kind, ttl, step_timeout, poll_interval, report
            )
            snapshot[name] = recycled_generation
            verdict = await _recycle_one(bus, name, recycled_generation, kind, report)
            # Recycled-so-far count (this target included) the convergence await must see
            # covered by NEW ready capacity.
            recycled = index + 1
            await _await_convergence(
                bus,
                kind,
                snapshot,
                name,
                recycled_generation,
                recycled,
                verdict,
                ttl,
                step_timeout,
                poll_interval,
                report,
            )
        # The new ready+fresh lives of this kind since the snapshot — informational capacity.
        census = await _census(
            bus, report, kind=kind, target=None, phase=f"reading the fresh capacity of the {kind.value} roll"
        )
        report.fresh.extend(_new_ready_lives(census, kind, snapshot, ttl))
    if applier_self_deferred:
        report.applier = ApplierEntry(name=excluded_name, generation=applier_generation)
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
    """Wait for a target to return ``state=ready`` with a fresh beat before the recycle op is published.

    Returns the generation observed when it becomes ready — the life the recycle
    op will actually target (a gap row, or a pinned worker absent from the
    census, that re-mints during the wait is recycled at its NEW generation). A
    target already ready+fresh returns its current generation at once. This
    ready-wait gets its OWN per-phase budget (reset here); if it lapses, the
    target is recorded :data:`TIMED_OUT` (at ``generation``, its last known life)
    and a loud :class:`RecycleTimeoutError` names the slot and its last-seen state.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + step_timeout
    while True:
        census = await _census(
            bus, report, kind=kind, target=(name, generation), phase=f"waiting for {name!r} to return to ready"
        )
        row = _row(census, name)
        if row is not None and row.state == WorkerState.ready and presence_fresh(row.pttl_ms, ttl):
            return row.generation
        if loop.time() >= deadline:
            last = row.state.value if row is not None else "absent"
            unsatisfied = f"gap-row target never returned to ready (last state: {last})"
            _stop(report, kind, (name, generation), TIMED_OUT, unsatisfied)
            raise RecycleTimeoutError(name, unsatisfied, report)
        await asyncio.sleep(poll_interval)


async def _recycle_one(
    bus: WorkerBus, name: str, generation: int, kind: WorkerKind, report: RecycleReport
) -> WorkerResult | None:
    """Publish the recycle op to a single target and record its row.

    Returns ``None`` when the worker answered ``applied``, or the publisher's
    COMPUTED verdict for a target that did not answer — both go on to the
    convergence await, which decides the row on reality. A bus-unreachable publish
    or a ``failed`` terminal records the row :data:`FAILED` (detail: the transport
    error or the worker's error) and raises :class:`RecycleError`. A reply that does
    not name the target is a bus contract violation and raises with no row.
    """
    result = await bus.publish({"op": "recycle"}, targets=[name], local=None)
    if not result.reachable:
        _stop(report, kind, (name, generation), FAILED, result.error or "bus unreachable")
        raise RecycleError(f"recycle: bus unreachable while recycling {name!r}: {result.error}", report)
    entry = next((r for r in result.results if r.name == name), None)
    if entry is None:
        raise RecycleError(f"recycle: worker {name!r} did not apply the recycle op (no reply from the target)", report)
    if entry.outcome is OpOutcome.failed:
        _stop(report, kind, (name, generation), FAILED, entry.error or "the worker reported the recycle op failed")
        raise RecycleError(f"recycle: worker {name!r} did not apply the recycle op ({entry.error})", report)
    report.rows.append(RecycleRow(name=name, kind=kind.value, generation_before=generation, status=RECYCLED))
    return None if entry.outcome is OpOutcome.applied else entry


async def _await_convergence(
    bus: WorkerBus,
    kind: WorkerKind,
    snapshot: dict[str, int],
    name: str,
    generation: int,
    recycled: int,
    verdict: WorkerResult | None,
    ttl: float,
    step_timeout: float,
    poll_interval: float,
    report: RecycleReport,
) -> None:
    """Await the two acceptance facts within a fresh per-step deadline.

    - **old life gone** for ``(name, generation)``: presence[name] is absent/expired OR
      shows a generation greater than the target's (its row vanishing after
      ``state=recycling`` is the absent case);
    - **counted ready+fresh capacity**: ready rows of ``kind`` past the freshness gate on
      the census now whose life is NEW vs the snapshot, at least ``recycled`` of them
      (covering the targets recycled so far). This is a census-now fact, not a biography —
      a survivor that re-mints past its claim TTL mid-roll can inflate the count, a
      documented window.

    ``verdict`` is the publisher's computed verdict when the target never confirmed
    the op (``None`` after an ``applied`` terminal); the same facts decide either way.
    On the deadline the row is marked :data:`TIMED_OUT` with a detail naming the
    unsatisfied fact (and the verdict, when there is one), and a loud
    :class:`RecycleTimeoutError` names which fact stayed unsatisfied.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + step_timeout
    said = (
        "the recycle op was applied by the target"
        if verdict is None
        else f"the recycle op was not confirmed by the target ({verdict.detail or verdict.outcome.value})"
    )
    while True:
        rows = await _census(
            bus,
            report,
            kind=kind,
            target=(name, generation),
            phase=f"awaiting convergence of {name!r}",
            said=said,
        )
        old_gone = _old_life_gone(rows, name, generation)
        fresh = len(_new_ready_lives(rows, kind, snapshot, ttl))
        if old_gone and fresh >= recycled:
            return
        if loop.time() >= deadline:
            if not old_gone:
                unsatisfied = "old life still present"
            else:
                unsatisfied = f"fresh READY capacity short ({fresh}/{recycled})"
            detail = unsatisfied if verdict is None else f"{unsatisfied}; {said}"
            _stop(report, kind, (name, generation), TIMED_OUT, detail)
            raise RecycleTimeoutError(name, unsatisfied, report)
        await asyncio.sleep(poll_interval)


def _old_life_gone(rows: list[WorkerRow], name: str, generation: int) -> bool:
    """Whether the target's old life is gone from the census.

    True when its presence row is absent/expired, or a generation greater than
    the target's holds the name (a replacement took the slot).
    """
    row = _row(rows, name)
    return row is None or row.generation > generation


def _new_ready_lives(rows: list[WorkerRow], kind: WorkerKind, snapshot: dict[str, int], ttl: float) -> list[FreshLife]:
    """The ready+fresh rows of ``kind`` whose life is NEW vs the snapshot.

    A name absent from it, or a generation greater than that name's snapshot
    generation. A ready-but-decayed row fails the freshness gate and is not
    counted as capacity, matching the ready+fresh discipline every other consumer
    applies.
    """
    return [
        FreshLife(name=row.name, kind=row.kind.value, generation=row.generation)
        for row in rows
        if row.kind is kind
        and row.state == WorkerState.ready
        and presence_fresh(row.pttl_ms, ttl)
        and (row.name not in snapshot or row.generation > snapshot[row.name])
    ]


def _stop(report: RecycleReport, kind: WorkerKind, target: tuple[str, int] | None, status: str, detail: str) -> None:
    """Record where the roll stopped and why — the one writer of a stop.

    With a target in hand its row (appended when none exists yet, at the given
    generation) takes ``status`` and ``detail``; the report's ``stopped`` record is
    always set.
    """
    name: str | None = None
    if target is not None:
        name, generation = target
        row = next((row for row in report.rows if row.name == name), None)
        if row is None:
            report.rows.append(
                RecycleRow(name=name, kind=kind.value, generation_before=generation, status=status, detail=detail)
            )
        else:
            row.status = status
            row.detail = detail
    report.stopped = RecycleStop(kind=kind.value, name=name, detail=detail)


async def _census(
    bus: WorkerBus,
    report: RecycleReport,
    *,
    kind: WorkerKind,
    target: tuple[str, int] | None,
    phase: str,
    said: str | None = None,
) -> list[WorkerRow]:
    """Read the census for the roll — every census read of the roll goes through here.

    A bus outage (one of the bus's :data:`TRANSPORT_ERRORS`) is the roll's stop: it is
    logged, recorded through :func:`_stop` (the target in hand, if any, as
    :data:`FAILED`, with ``said`` — what the bus had said of the op — appended) and
    raised as :class:`RecycleError`. Any other error is a bus fault, not an outage,
    and propagates unchanged.
    """
    try:
        return await bus.census()
    except TRANSPORT_ERRORS as exc:
        detail = f"bus unreachable while {phase}: {type(exc).__name__}: {exc}"
        if said is not None:
            detail = f"{detail}; {said}"
        logger.exception("recycle: bus unreachable while %s", phase)
        _stop(report, kind, target, FAILED, detail)
        raise RecycleError(f"recycle: {detail}", report) from exc


def _row(rows: list[WorkerRow], name: str) -> WorkerRow | None:
    return next((row for row in rows if row.name == name), None)
