"""Recycle orchestration — the rolling per-kind recycle loop.

Called once a change's reload report decided which workers need a restart and which must be
asked first (:func:`~tai42_skeleton.config.recycle_policy.recycle_decision`). It recycles the
fleet ONE worker at a time per kind, and confirms each step on REALITY rather than on a
successor's identity: after publishing the recycle op to a target it
awaits two facts — the target's OLD life is gone, and enough NEW READY capacity of that
kind has joined to cover the recycled targets so far. Rolling one kind at a time keeps
the fleet from losing every worker of a kind at once.

A worker's WIRE terminal is an answer: ``applied`` is confirmed on reality, ``failed``
records the target :data:`FAILED` with the worker's error. A verdict the publisher
COMPUTED from its own bus reads (``missing`` / ``departed`` / ``timed_out`` from the slot
claim, or a gap outcome from the presence row) is not an answer — the target may still
take the op up once it is free — so the target is awaited on the same acceptance facts
within the step budget. A target that answers its exit
is deferred to its own in-flight change is recorded :data:`SELF_DEFERRED` and not awaited:
that change's reply is answered first and its own exit restarts it. The roll stops at the
first target that is not :data:`RECYCLED`, :data:`SELF_DEFERRED` or :data:`WITHHELD`, or when the bus
cannot be read (an outage on any of the roll's census reads, with or without a target in
hand); later targets and the applier's own recycle are not attempted, and the report's
``stopped`` record names where and why.

Each target is acted on as the life that is ready when the roll reaches it, never on the
report's earlier sample alone. A target whose reload reported recycle-class keys is
recycled without a further ask only when the ready life is the one that reported them
(same name and generation). Every other target — its state unknown at the report (booting,
resyncing, mid-recycle, busy with its own reload, joined after the change began), or the
life that reported keys gone — is first asked with a targeted ``reload_config`` and its
reply decides (:func:`~tai42_skeleton.config.recycle_policy.judge_row`): confirmed clean, it
is left running (``confirmed_clean``); refused at its epoch build yet reporting keys, it is left
running and recorded :data:`WITHHELD` with them (a restart may fail the same way);
reporting keys, it is recycled with them; still not
stating its env when asked directly, it is recycled as unknown, its row's ``detail`` naming
both asks. A target whose life ends during the ask is waited back to ready and its next
life asked, within the same ready budget.

The targets of a kind are the workers pinned at the apply's start unioned with the
census at roll time, the ones the decision rolls over (not the applier, not the workers
that confirmed they need no restart, and, for a reload naming its targets, only those),
each recorded as ``(name, generation)`` against a per-kind snapshot, so a
replacement that takes over the freed slot name (at a higher generation) is recognised
as a NEW life, not the same worker. A pinned worker absent
at roll time (its slot lost and re-minting) is waited to ready and recycled at the
generation it returns with. The report names each target with its
``generation_before``, its status and a ``detail`` for a target that did not
converge, and a per-kind ``fresh`` list of the new lives observed since the snapshot —
informational, never claimed as any target's successor and never carrying a
post-recycle generation.

A target costs at most the ready-wait budget, one bus apply timeout for the ask, one for
the recycle op, and the step budget; nothing waits unbounded. A life the roll sees leaving
(``state=recycling``) that announced how long it may take to finish its in-flight work and
exit (its row's ``drain_seconds``) extends the wait it is in to that drain plus the step
budget, counted from the first sight of it leaving and taken once per life
(:class:`_StepDeadline`): a worker only finishing its jobs is waited for, and one that stays
past its drain still times out, its detail naming the drain it announced.

The applying worker is never a target: the bus echo-skips the publisher's own frames,
so the applier can never handle its own recycle op. When the applier needs its own
restart, it is a deferred post-response self-exit it cannot confirm, reported as an
``applier`` entry with the :data:`SELF_DEFERRED` status once every target converged.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

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
from tai42_skeleton.app.bus.ops import Recycle, ReloadConfig, recycle_leaving
from tai42_skeleton.config.recycle_policy import judge_row

if TYPE_CHECKING:
    from tai42_skeleton.config.recycle_policy import RecycleDecision

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
"""A worker whose restart is its own deferred post-reply exit, which the roll cannot confirm:
the applier self-entry status in a recycle report (the applying serve worker's own restart),
and a target row's status when the target answered that its exit is deferred to its own
in-flight change (it leaves once that change's reply is answered; the roll does not wait for
it). One shared literal, so the status never diverges across its readers."""

WITHHELD = "withheld"
"""A target asked once ready whose reload was refused at its epoch build yet reports recycle-class
keys off their bound value: the stored config did not build on it and a restart may fail the same
way, so the roll leaves it running on its current config and makes no restart for those keys, which
the verdict names."""

# Recycle one KIND at a time in this order: a backend replacement must be serving before
# serve workers roll, so no kind loses every worker at once.
_KIND_ORDER: tuple[WorkerKind, ...] = (WorkerKind.backend, WorkerKind.serve)


class RecycleRow(BaseModel):
    """One target's outcome: its pre-recycle identity, why it was restarted, and its terminal status.

    The status is :data:`RECYCLED` once convergence is confirmed, :data:`TIMED_OUT`
    on the step budget, :data:`FAILED` when the worker reported the op failed or
    the bus could not deliver it, :data:`SELF_DEFERRED` when the target's exit is
    deferred to its own in-flight change, or :data:`WITHHELD` when the target, asked once
    ready, was refused at its epoch build yet reports keys, and was left running.
    ``generation_before`` is the life that was recycled or left running — never a
    post-recycle life. ``keys`` are the recycle-class keys the
    target stated as moved off their bound value (at the reload, or when asked once
    ready); empty for a target restarted because its state stayed unknown. ``detail``
    names what left its state unknown (the report's reason and, once asked, its answer),
    that the target was already leaving when the recycle op reached it
    (``"already leaving: <reason>"``) or had its exit deferred to its own change
    (``"its exit is deferred to its own in-flight change: <change>"``), and why a target did
    not converge (the unsatisfied fact, plus the bus verdict when the target never confirmed
    the op; the worker's error; the transport error), joined with ``"; "``; ``None`` when
    neither applies.
    """

    name: str
    kind: str
    generation_before: int
    status: str
    keys: list[str] = Field(default_factory=list)
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
    """The applier's own deferred self-exit entry, present only when the applier needs its own restart.

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

    ``rows`` is one entry per target the roll recycled, withheld or stopped at (its ``generation_before``,
    keys, terminal status and detail), ending at the first one that did not converge;
    ``confirmed_clean`` names the targets that, asked once ready, confirmed no recycle-class key off
    its bound value and were left running; ``fresh`` is the new READY lives observed since the
    pre-apply snapshot, per kind — evidence of capacity, never a claimed successor; ``applier``
    (when set) is the applier's deferred self-exit, present only on a converged roll.
    """

    rows: list[RecycleRow] = Field(default_factory=list)
    confirmed_clean: list[str] = Field(default_factory=list)
    fresh: list[FreshLife] = Field(default_factory=list)
    applier: ApplierEntry | None = None
    stopped: RecycleStop | None = None

    @property
    def converged(self) -> bool:
        """Whether the roll ran to its end: no stop recorded, every target recycled, self-deferred or withheld.

        Every row is :data:`RECYCLED`, :data:`SELF_DEFERRED` or :data:`WITHHELD`.
        A self-deferred target leaves through its own change's exit, which the roll does not
        wait for. A withheld target owes a restart the roll decided not to make; the verdict
        states it, the roll does not await it. The applier entry does not count.
        """
        return self.stopped is None and all(row.status in (RECYCLED, SELF_DEFERRED, WITHHELD) for row in self.rows)


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
    decision: RecycleDecision,
    target_kinds: Sequence[WorkerKind],
    applier_self_deferred: bool,
    step_timeout: float,
    expected: Mapping[str, tuple[WorkerKind, int]] | None = None,
    poll_interval: float = 0.2,
) -> RecycleReport:
    """Roll a recycle across the fleet, kind by kind, over the workers ``decision`` rolls over.

    ``decision`` names the applier (its ``writer``, never a target), the workers that reported
    keys with the life that reported them, the ones whose state is unknown and why, and which
    workers the roll visits (:meth:`~tai42_skeleton.config.recycle_policy.RecycleDecision.rolls_over`).
    ``expected`` is the membership pinned at the apply's start, ``name -> (kind, generation)``,
    applier excluded. For each targeted kind, snapshot the live lives (``name -> generation``)
    and take as targets the pinned names of the kind unioned with the census rows of the kind
    the decision rolls over: a row left out, being in the snapshot, never counts as fresh
    capacity. Then visit them one at a time: a target not ready+fresh (a gap row, or a pinned
    worker absent from the census) is first waited to ``ready`` on its own budget; the ready
    life is recycled at once when it is the life that reported keys, else asked first
    (:func:`_confirm`) and left running when it confirms clean or its restart is withheld
    (:data:`WITHHELD`). A recycled target's recycle op
    is published to it alone, and convergence is awaited — the target's old life gone AND
    enough new READY capacity of the kind to cover the targets recycled so far. A ``failed``
    terminal or an undeliverable op records the target :data:`FAILED` and raises
    :class:`RecycleError`; a convergence or ready-wait not confirmed within ``step_timeout``
    (extended by the drain a departing life announced, :class:`_StepDeadline`) records it
    :data:`TIMED_OUT` and raises :class:`RecycleTimeoutError`. Every census read goes through
    :func:`_census`: a bus outage on one stops the roll — the target in hand, if any, recorded
    :data:`FAILED` — and raises :class:`RecycleError`. Each carries the partial report with its
    ``stopped`` record. The applier entry (``applier_self_deferred``) is set
    only once every target converged.
    """
    report = RecycleReport()
    pinned = expected or {}
    applier = decision.writer
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
            if pinned_kind is kind and decision.rolls_over(name) and name not in snapshot
        ]
        # Roll-time targets: every row of this kind the decision rolls over, captured as
        # (name, generation) so a replacement taking over the freed name is a distinct life.
        present = [(row.name, row.generation) for row in rows if decision.rolls_over(row.name)]
        recycled = 0
        for name, generation in [*absent, *present]:
            # A target not ready+fresh is first waited to ready on its OWN per-phase
            # budget, so a slow ready-wait never starves the convergence await. The
            # generation observed when it becomes ready is the life acted on.
            restart = await _ready_restart(
                bus, decision, name, generation, kind, ttl, step_timeout, poll_interval, report
            )
            if restart is None:
                continue
            ready_generation, keys, detail = restart
            snapshot[name] = ready_generation
            row, verdict = await _recycle_one(bus, name, ready_generation, kind, keys, detail, report)
            if row.status == SELF_DEFERRED:
                # Its own change's exit restarts it after that change's reply: nothing to await.
                continue
            # Recycled-so-far count (this target included) the convergence await must see
            # covered by NEW ready capacity.
            recycled += 1
            await _await_convergence(
                bus,
                kind,
                snapshot,
                name,
                ready_generation,
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
        report.applier = ApplierEntry(name=applier.name, generation=applier.generation)
    return report


async def _ready_restart(
    bus: WorkerBus,
    decision: RecycleDecision,
    name: str,
    generation: int,
    kind: WorkerKind,
    ttl: float,
    step_timeout: float,
    poll_interval: float,
    report: RecycleReport,
) -> tuple[int, list[str], str | None] | None:
    """Wait a target to ready and decide on that life: ``(generation, keys, detail)`` recycles it, ``None`` leaves it.

    The life that becomes ready is recycled at once when it is the life whose reload reported
    keys (same generation). Any other life is asked (:func:`_confirm`): one that confirms clean
    is recorded in ``confirmed_clean`` and left running; one whose reload is refused at its epoch
    build yet reports keys is recorded :data:`WITHHELD` with them and left running; one whose
    life ends during the ask is waited back to ready and its next life asked, all within the target's one ready budget.
    """
    deadline = _StepDeadline(step_timeout)
    reported = decision.reported.get(name)
    while True:
        ready_generation = await _await_ready(bus, name, generation, kind, ttl, deadline, poll_interval, report)
        if reported is not None and reported.generation == ready_generation:
            return ready_generation, list(reported.keys), None
        answer = await _confirm(bus, name, ready_generation, kind, report)
        if answer is None:
            # The life ended during the ask: its next life is the one to ask.
            generation = ready_generation
            continue
        reason = judge_row(answer)
        first = decision.why_unconfirmed(name)
        if reason is None:
            logger.info(
                "recycle: %r (%s) confirmed clean once ready (generation %d) — left running",
                name,
                first,
                ready_generation,
            )
            report.confirmed_clean.append(name)
            return None
        if reason.withheld is not None:
            logger.info(
                "recycle: %r (%s) owes a respawn for %s once ready (generation %d) but %s — left running",
                name,
                first,
                ", ".join(reason.keys),
                ready_generation,
                reason.withheld,
            )
            report.rows.append(
                RecycleRow(
                    name=name,
                    kind=kind.value,
                    generation_before=ready_generation,
                    status=WITHHELD,
                    keys=list(reason.keys),
                    detail=f"{first}; asked once ready: {reason.withheld}",
                )
            )
            return None
        if reason.unknown is None:
            return ready_generation, list(reason.keys), f"{first}; confirmed once ready"
        return ready_generation, [], f"{first}; asked once ready: {reason.unknown}"


async def _confirm(
    bus: WorkerBus, name: str, generation: int, kind: WorkerKind, report: RecycleReport
) -> WorkerResult | None:
    """Ask a ready target to state its env with a targeted ``reload_config``: its reply row, ``None`` if its life ended.

    A ``departed`` reply means the life asked is gone, so the caller asks the next one. A
    bus-unreachable publish records the target :data:`FAILED` and raises :class:`RecycleError`;
    a reply that does not name the target is a bus contract violation and raises with no row.
    """
    result = await bus.publish(ReloadConfig(op="reload_config"), targets=[name], local=None)
    if not result.reachable:
        _stop(report, kind, (name, generation), FAILED, result.error or "bus unreachable")
        raise RecycleError(f"recycle: bus unreachable while asking {name!r} for its env: {result.error}", report)
    entry = next((r for r in result.results if r.name == name), None)
    if entry is None:
        raise RecycleError(f"recycle: worker {name!r} did not answer the reload it was asked (no reply row)", report)
    return None if entry.outcome is OpOutcome.departed else entry


async def _await_ready(
    bus: WorkerBus,
    name: str,
    generation: int,
    kind: WorkerKind,
    ttl: float,
    deadline: _StepDeadline,
    poll_interval: float,
    report: RecycleReport,
) -> int:
    """Wait for a target to return ``state=ready`` with a fresh beat before it is asked or recycled.

    Returns the generation observed when it becomes ready — the life the roll acts
    on (a gap row, or a pinned worker absent from the census, that re-mints during
    the wait is acted on at its NEW generation). A target already ready+fresh
    returns its current generation at once. The ready-wait runs to ``deadline``, the
    target's OWN ready budget, extended by the drain a life of it seen leaving announced
    (:class:`_StepDeadline`); if it lapses, the target is recorded
    :data:`TIMED_OUT` (at ``generation``, its last known life) and a loud
    :class:`RecycleTimeoutError` names the slot and its last-seen state.
    """
    while True:
        census = await _census(
            bus, report, kind=kind, target=(name, generation), phase=f"waiting for {name!r} to return to ready"
        )
        row = _row(census, name)
        if row is not None and row.state == WorkerState.ready and presence_fresh(row.pttl_ms, ttl):
            return row.generation
        deadline.observe(row)
        if deadline.lapsed():
            last = row.state.value if row is not None else "absent"
            unsatisfied = deadline.unsatisfied(f"gap-row target never returned to ready (last state: {last})")
            _stop(report, kind, (name, generation), TIMED_OUT, unsatisfied)
            raise RecycleTimeoutError(name, unsatisfied, report)
        await asyncio.sleep(poll_interval)


async def _recycle_one(
    bus: WorkerBus,
    name: str,
    generation: int,
    kind: WorkerKind,
    keys: list[str],
    detail: str | None,
    report: RecycleReport,
) -> tuple[RecycleRow, WorkerResult | None]:
    """Publish the recycle op to a single target and record its row, carrying why it is restarted.

    ``keys`` and ``detail`` are what the target stated, or what left its state unknown.

    Returns the recorded row with ``None`` when the worker answered ``applied``, or with the
    publisher's COMPUTED verdict for a target that did not answer — both go on to the
    convergence await, which decides the row on reality. A target that answers it was
    already leaving (``already_leaving`` in its ``applied`` payload) is awaited the same way,
    its row's detail naming that reason. A target that answers its exit is deferred to its own
    in-flight change (``exit_deferred``) is recorded :data:`SELF_DEFERRED`, its detail naming
    that change, and is not awaited: its own exit follows that change's reply. A bus-unreachable publish
    or a ``failed`` terminal records the row :data:`FAILED` (detail: the transport
    error or the worker's error) and raises :class:`RecycleError`. A reply that does
    not name the target is a bus contract violation and raises with no row.
    """
    row = RecycleRow(
        name=name, kind=kind.value, generation_before=generation, status=RECYCLED, keys=keys, detail=detail
    )
    result = await bus.publish(Recycle(op="recycle"), targets=[name], local=None)
    if not result.reachable:
        report.rows.append(row)
        _stop(report, kind, (name, generation), FAILED, result.error or "bus unreachable")
        raise RecycleError(f"recycle: bus unreachable while recycling {name!r}: {result.error}", report)
    entry = next((r for r in result.results if r.name == name), None)
    if entry is None:
        raise RecycleError(f"recycle: worker {name!r} did not apply the recycle op (no reply from the target)", report)
    leaving = recycle_leaving(entry.payload) if entry.outcome is OpOutcome.applied else None
    if leaving is not None:
        if leaving.kind == "exit_deferred":
            row.status = SELF_DEFERRED
            said = f"its exit is deferred to its own in-flight change: {leaving.reason}"
        else:
            said = f"already leaving: {leaving.reason}"
        row.detail = "; ".join(part for part in (detail, said) if part)
    report.rows.append(row)
    if entry.outcome is OpOutcome.failed:
        _stop(report, kind, (name, generation), FAILED, entry.error or "the worker reported the recycle op failed")
        raise RecycleError(f"recycle: worker {name!r} did not apply the recycle op ({entry.error})", report)
    return row, None if entry.outcome is OpOutcome.applied else entry


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
    """Await the two acceptance facts within a fresh per-step deadline, extended by an announced drain.

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
    The deadline is ``step_timeout`` from now, extended by the drain the target's life
    announced when it is seen leaving (:class:`_StepDeadline`). On the deadline the row is
    marked :data:`TIMED_OUT` with a detail naming the unsatisfied fact (and the verdict, when there is one), and a loud
    :class:`RecycleTimeoutError` names which fact stayed unsatisfied.
    """
    deadline = _StepDeadline(step_timeout)
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
        deadline.observe(_row(rows, name))
        if deadline.lapsed():
            if not old_gone:
                unsatisfied = deadline.unsatisfied("old life still present")
            else:
                unsatisfied = deadline.unsatisfied(f"fresh READY capacity short ({fresh}/{recycled})")
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


class _StepDeadline:
    """One wait of the roll: the step budget, extended by the drain a departing life announced.

    The deadline starts ``step_timeout`` from now. A life seen leaving (``state=recycling``) with
    an announced ``drain_seconds`` may take that long to finish its in-flight work and exit before
    its replacement even starts, so the deadline moves to that drain plus the step budget from the
    first sight of it — once per life, so a life that stays past it still lapses.
    """

    def __init__(self, step_timeout: float) -> None:
        self._loop = asyncio.get_running_loop()
        self._step = step_timeout
        self._at = self._loop.time() + step_timeout
        self._seen: set[int] = set()
        self._drain: float | None = None

    def observe(self, row: WorkerRow | None) -> None:
        """Extend the deadline by ``row``'s announced drain the first time its life is seen leaving."""
        if row is None or row.state is not WorkerState.recycling or row.drain_seconds is None:
            return
        if row.generation in self._seen:
            return
        self._seen.add(row.generation)
        self._drain = row.drain_seconds
        self._at = max(self._at, self._loop.time() + row.drain_seconds + self._step)

    def lapsed(self) -> bool:
        """Whether the wait has run past its deadline."""
        return self._loop.time() >= self._at

    def unsatisfied(self, fact: str) -> str:
        """The fact left unsatisfied, naming the announced drain the wait covered, when one extended it."""
        return fact if self._drain is None else f"{fact} after its announced drain of {self._drain:g}s"


def _stop(report: RecycleReport, kind: WorkerKind, target: tuple[str, int] | None, status: str, detail: str) -> None:
    """Record where the roll stopped and why — the one writer of a stop.

    With a target in hand its row (appended when none exists yet, at the given
    generation) takes ``status``, and ``detail`` joins the row's own detail (what left
    its state unknown, when it has one); the report's ``stopped`` record is always set.
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
            row.detail = detail if row.detail is None else f"{row.detail}; {detail}"
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
