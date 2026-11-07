"""The fleet recycle a reload owes: decide from its report whom to restart, roll over them, judge the verdict.

Every operation that starts a reload — each config pipeline run, a deferred marketplace scope,
the fleet soft restart and a joining worker's announcement — ends here
(:func:`decide_and_roll`): the reload's report decides which workers need a restart
(:func:`~tai42_skeleton.config.recycle_policy.recycle_decision`), the recycle orchestrator rolls
over them one at a time (:func:`roll_decided`), and the workers still awaiting a respawn
afterwards are the operation's verdict. A change run under a bound the roll's waits can outlast
hands the roll to the bus member it acts for, which rolls once the change's run ended.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence, Set
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tai42_kit.utils.run_bound import run_bound

from tai42_skeleton.app.recycle import WITHHELD, RecycleError, RecycleReport, RecycleStop
from tai42_skeleton.config.recycle_policy import (
    CENSUS_TARGET_KINDS,
    UNREACHABLE_REPORT,
    RecycleDecision,
    RecycleReason,
    recycle_decision,
    recycle_step_timeout,
)

if TYPE_CHECKING:
    from tai42_contract.app.responses import AwaitingWorker

    from tai42_skeleton.app.bus import FleetResult, WorkerBus, WorkerIdentity, WorkerKind, WorkerRow
    from tai42_skeleton.app.writer_exit import WriterExitScope

logger = logging.getLogger(__name__)

Orchestrate = Callable[..., Awaitable[RecycleReport]]
"""The recycle orchestrator's signature (:func:`~tai42_skeleton.app.recycle.orchestrate_recycle`)."""


@dataclass(frozen=True)
class RollOutcome:
    """What a recycle decision did: the decision, its roll's report, the verdict and the writer's exit.

    ``recycle`` is ``None`` when no roll ran here (the roll had no worker to visit: every worker's
    reload confirmed no recycle-class key, or the only ones naming one were refused at their epoch build
    or are the writer whose own restart cannot follow as its exit; the roll was deferred to a bus member, or it failed:
    :class:`RollFailedError`); ``self_exit_armed`` is whether the writer's own post-reply
    self-exit follows the roll — the one owner of that fact every result carries.
    ``deferred_to`` names the bus member a bounded change deferred its roll to: one handed off,
    which that member carries out on its own and judges, or one that may have been sent with its
    answer not known, which is left to that member to carry out and judge. ``awaiting_respawn`` is
    then empty; ``deferred_to`` is ``None`` when the roll was judged here.
    """

    decision: RecycleDecision
    recycle: RecycleReport | None
    awaiting_respawn: list[AwaitingWorker]
    self_exit_armed: bool
    deferred_to: str | None = None

    @property
    def deferred(self) -> bool:
        """Whether the roll is not judged in this outcome: it is deferred to the member ``deferred_to`` names.

        Deferred means handed off, which that member carries out and judges, or may have been sent
        with its answer not known, which is left to that member to carry out and judge.
        """
        return self.deferred_to is not None


class RollFailedError(RuntimeError):
    """The roll a recycle decision owes raised before it ended: its hand-off failed, or the roll itself raised.

    ``failure`` is what raised, the error's cause, whose text the error reads. ``outcome`` is
    what the operation stands at: the decision, no roll report and the writer's own exit not
    armed. A roll that may have been sent to the bus member a bounded change acts for, whose
    answer is not known (:class:`~tai42_skeleton.config.roll_handoff.RollHandOffUnknownError`),
    may be running there, so it is not judged here: ``outcome`` names that member as
    ``deferred_to`` and lists no worker awaiting a respawn. A roll that did not start (its
    hand-off refused) or raised here is judged from the reload's report alone — every worker
    whose reload row names a recycle-class key off its bound value is still listed awaiting a
    respawn. A config write's settle and a reload whose propagation failed carry ``outcome`` as
    their raised :class:`~tai42_skeleton.operations._broadcast.FleetBroadcastError`'s roll. A soft
    restart whose reload succeeded raises this error itself, and no reply reads its ``outcome``; a
    marketplace operation's :class:`~tai42_skeleton.marketplace.errors.UnrunRollError` drops it and
    keeps only the failure's type name.
    """

    def __init__(self, failure: Exception, outcome: RollOutcome) -> None:
        """Build the error from the ``failure`` that raised and the ``outcome`` it leaves."""
        super().__init__(str(failure))
        self.failure = failure
        self.outcome = outcome


def pinned_recycle_targets(rows: list[WorkerRow], writer_name: str) -> dict[str, tuple[WorkerKind, int]]:
    """The recycle targets pinned at an operation's start, ``name -> (kind, generation)``.

    Every census row of a recycle target kind except the writer, gap rows included:
    the roll waits a target that is not ready back to ready before recycling it.
    """
    return {
        row.name: (row.kind, row.generation)
        for row in rows
        if row.kind in CENSUS_TARGET_KINDS and row.name != writer_name
    }


async def decide_and_roll(
    bus: WorkerBus,
    report: FleetResult,
    writer: WorkerIdentity,
    pinned: Mapping[str, tuple[WorkerKind, int]],
    *,
    change: str,
    orchestrate: Orchestrate,
    writer_exits: bool = True,
    addressed: Set[str] | None = None,
    holds: Sequence[str] = (),
) -> RollOutcome:
    """Decide from ``report`` which workers need a restart, then roll the recycle over them or hand the roll off.

    A worker that reported recycle-class keys is restarted; a sibling whose state the report leaves
    unknown is asked once it is ready and restarted unless its reply confirms it clean or states a
    refused build (its restart then withheld); the writer is never asked, and when its own state is
    unknown it needs its own restart; one whose reload was refused at its epoch build yet reports
    keys is left running, its restart withheld and named in the verdict
    (:func:`~tai42_skeleton.config.recycle_policy.recycle_decision`); the decision is logged at INFO
    naming ``change``. ``addressed`` restricts the decision and the roll to the workers a reload
    naming its targets reached; every other worker is left running, never asked. ``writer_exits``
    says whether the writer's own restart can follow as its exit once the operation answers (a
    caller whose reply cannot arm it passes ``False``).

    A change run under a bound its roll's waits can outlast
    (:func:`~tai42_kit.utils.run_bound.run_bound`: a dispatched job's budget, an armed turn
    budget) never runs a roll itself: each target's drain, and the writer's own exit, which waits
    for every other change holding the worker's exit, are waits the bound cannot hold. It hands
    the roll to the bus member it acts for
    (:func:`~tai42_skeleton.config.roll_handoff.hand_off_roll`), which carries it out as its own
    task once the change's run ended — the bound's exit releases it — and departs itself last, once
    the roll converged, when the writer needs
    its restart, and returns with ``deferred_to`` naming that member. The member keeps the roll holds ``holds`` names
    (:mod:`~tai42_skeleton.config.roll_holds`) from before its answer until the roll ended: what
    the caller holds across its roll, which it releases once it answers. Every other roll runs here
    (:func:`roll_decided`), under what the caller holds itself; a bounded decision with no roll to
    run returns at once.

    A hand-off that fails, or a roll that raises, raises :class:`RollFailedError` from that
    failure, carrying the outcome the operation stands at: the roll deferred to the member when
    it may have been sent there with its answer not known (:func:`_deferred_roll_outcome`), else
    the reload report's own verdict (:func:`_failed_roll_outcome`).
    """
    from tai42_skeleton.config.roll_handoff import RollHandOffUnknownError, hand_off_roll

    decision = recycle_decision(report, pinned, writer, addressed=addressed)
    _log_decision(decision, change=change)
    try:
        bound = run_bound()
        if bound is not None and _roll_runs(decision, writer_exits=writer_exits):
            member = await hand_off_roll(
                bus,
                report,
                writer,
                pinned,
                bound=bound,
                change=change,
                writer_exits=writer_exits,
                addressed=addressed,
                holds=holds,
            )
            return _deferred_roll_outcome(decision, member)
        return await roll_decided(
            bus, report, decision, pinned, change=change, orchestrate=orchestrate, writer_exits=writer_exits
        )
    except RollHandOffUnknownError as unknown:
        raise RollFailedError(unknown, _deferred_roll_outcome(decision, unknown.member)) from unknown
    except Exception as failure:
        raise RollFailedError(failure, _failed_roll_outcome(report, decision)) from failure


def _deferred_roll_outcome(decision: RecycleDecision, member: str) -> RollOutcome:
    """The outcome of a decision whose roll is deferred to the bus member ``member``: not judged here.

    The roll was handed off to ``member``, which carries it out and judges it, or may have been
    sent to it with its answer not known, which leaves it to ``member`` to carry out and judge.
    Either way no roll report, no exit armed here and no worker listed awaiting a respawn.
    """
    return RollOutcome(decision=decision, recycle=None, awaiting_respawn=[], self_exit_armed=False, deferred_to=member)


def _failed_roll_outcome(report: FleetResult, decision: RecycleDecision) -> RollOutcome:
    """The outcome a decision whose roll did not start, or raised here, stands at: the report's own verdict.

    No roll report and no exit armed. No roll is judged, so the verdict is what the reload's
    ``report`` states (:func:`~tai42_skeleton.operations._broadcast.awaiting_respawn_after` with no roll).
    """
    from tai42_skeleton.operations._broadcast import awaiting_respawn_after

    verdict = awaiting_respawn_after(report, None, decision.writer, self_exit_armed=False)
    return RollOutcome(decision=decision, recycle=None, awaiting_respawn=verdict, self_exit_armed=False)


async def roll_decided(
    bus: WorkerBus,
    report: FleetResult,
    decision: RecycleDecision,
    pinned: Mapping[str, tuple[WorkerKind, int]],
    *,
    change: str,
    orchestrate: Orchestrate,
    writer_exits: bool,
    exits: WriterExitScope | None = None,
) -> RollOutcome:
    """Roll the recycle ``decision`` names over the fleet, arm the writer's exit, and judge the verdict.

    When ``writer_exits`` and the writer needs its own restart, the roll defers it to the writer's
    exit once the operation answers. The exit is owed into ``exits``, or into the open writer-exit
    scope the edge answering the operation settles when ``exits`` is ``None``
    (:mod:`~tai42_skeleton.app.writer_exit`); the change's hold on the writer's exit from its start
    (:func:`~tai42_skeleton.app.writer_exit.hold_writer_exit`) is reused, and taken here when the
    change's door took none, so a recycle op from another worker's roll leaves the writer to it.
    Deferring with no scope open raises before anything rolls, since no edge would run the exit. A
    converged roll arms the exit; a roll that does not (it stopped, or raised) owes nothing, and
    the scope releases the hold once the edge answered. The roll runs when a worker reported keys
    or is to be asked, the report reached no worker (the roll's census names whom to ask), or the
    writer's deferred restart does (with no target it converges at once and arms the exit);
    otherwise it is skipped with an INFO line (:func:`_log_skipped_roll`). The verdict is logged at ERROR
    when it names a worker.
    """
    from tai42_skeleton.app.writer_exit import open_writer_exit_scope
    from tai42_skeleton.operations._broadcast import awaiting_respawn_after, log_awaiting_respawn

    writer = decision.writer
    writer_deferred = writer_exits and decision.writer_needs
    # The scope the exit this roll arms is owed into. The change's hold on the writer's exit is
    # reused; a door that took none takes it here.
    scope = None
    if writer_deferred:
        scope = exits if exits is not None else open_writer_exit_scope(change)
        scope.hold_exit(bus, change)
    recycle: RecycleReport | None = None
    armed = False
    if _roll_runs(decision, writer_exits=writer_exits):
        recycle = await _roll_recycle(
            bus, decision, pinned, writer_self_deferred=writer_deferred, change=change, orchestrate=orchestrate
        )
    else:
        _log_skipped_roll(decision, change=change)
    if scope is not None and recycle is not None and recycle.converged:
        # Only a converged roll arms the writer's exit: a roll that stopped never recycles
        # anything past the row it stopped at, the writer's own restart included.
        scope.owe(bus)
        armed = True
    verdict = awaiting_respawn_after(report, recycle, writer, self_exit_armed=armed)
    log_awaiting_respawn(verdict, withheld=_withheld_names(decision, recycle))
    return RollOutcome(decision=decision, recycle=recycle, awaiting_respawn=verdict, self_exit_armed=armed)


def _withheld_names(decision: RecycleDecision, recycle: RecycleReport | None) -> set[str]:
    """The workers whose restart ``decision`` and its roll withhold: their reload was refused at their epoch build.

    The workers the decision withheld, the writer when its own restart is withheld, and every
    target the roll recorded :data:`~tai42_skeleton.app.recycle.WITHHELD` when it asked it once ready.
    """
    names = set(decision.withheld)
    if decision.writer_reason is not None and decision.writer_reason.withheld is not None:
        names.add(decision.writer.name)
    if recycle is not None:
        names |= {row.name for row in recycle.rows if row.status == WITHHELD}
    return names


def _roll_runs(decision: RecycleDecision, *, writer_exits: bool) -> bool:
    """Whether ``decision`` has a roll to run.

    A worker reported keys or is to be asked, the report reached no worker (the roll's own census
    names whom to ask), or the writer exits after the reply and needs its own restart.
    """
    return (
        bool(decision.reported or decision.unconfirmed)
        or decision.report_unreachable
        or (writer_exits and decision.writer_needs)
    )


async def _roll_recycle(
    bus: WorkerBus,
    decision: RecycleDecision,
    pinned: Mapping[str, tuple[WorkerKind, int]],
    *,
    writer_self_deferred: bool,
    change: str,
    orchestrate: Orchestrate,
) -> RecycleReport:
    """Roll a recycle across the fleet over the workers ``decision`` rolls over, asking first those it left unknown.

    The pinned targets are expected, so one absent from the roll-time census is waited back to
    ready. The writer is left out of the roll (``writer_self_deferred``); whether its own exit is
    armed is decided after the roll, by :func:`roll_decided`. A roll that stops — on a target, or
    because the bus could not be read — is logged at ERROR naming ``change`` and returned as the
    partial report, never raised: a stop is an outcome of the roll. A raise with no recorded stop is
    a bus contract violation, not an outcome of the roll, and escapes (as :func:`decide_and_roll`'s
    :class:`RollFailedError`) to the door that ran the roll, which states what its change persisted.
    """
    try:
        return await orchestrate(
            bus,
            decision=decision,
            target_kinds=CENSUS_TARGET_KINDS,
            applier_self_deferred=writer_self_deferred,
            step_timeout=recycle_step_timeout(),
            expected=dict(pinned),
        )
    except RecycleError as exc:
        stopped = exc.report.stopped
        if stopped is None:
            raise
        _log_stopped_roll(exc.report, stopped, pinned, change=change)
        return exc.report


def _log_skipped_roll(decision: RecycleDecision, *, change: str) -> None:
    """INFO-log that the recycle for ``change`` is skipped, naming the clean and withheld workers and the writer.

    A skipped roll restarts nobody: every worker it would visit confirmed clean or has its restart
    withheld, and the writer is clean, has its restart withheld, or needs its own restart with no
    exit to follow it. Each line names the workers whose restart is withheld, with their keys.
    """
    clean = ", ".join(sorted(decision.clean)) or "none"
    withheld = _withheld_text(decision)
    writer_reason = decision.writer_reason
    if writer_reason is not None and writer_reason.withheld is not None:
        logger.info(
            "the fleet recycle for %s is skipped: no restart the roll makes is owed; the writer %r %s "
            "(confirmed clean: %s; left running, its restart withheld: %s)",
            change,
            decision.writer.name,
            _reason_text(writer_reason),
            clean,
            withheld,
        )
        return
    if decision.writer_needs:
        logger.info(
            "the fleet recycle for %s is skipped: only the writer %r needs a restart, which no exit follows "
            "(confirmed clean: %s; left running, its restart withheld: %s)",
            change,
            decision.writer.name,
            clean,
            withheld,
        )
        return
    logger.info(
        "the fleet recycle for %s is skipped: no worker needs a restart (confirmed clean: %s; left running, its "
        "restart withheld: %s; the writer %r is clean)",
        change,
        clean,
        withheld,
        decision.writer.name,
    )


def _log_decision(decision: RecycleDecision, *, change: str) -> None:
    """INFO-log whom the recycle for ``change`` restarts and why, asks once ready, leaves running, and the writer.

    A report that reached no worker leaves whom to ask to the roll's census — of the workers the
    reload named, when it named its targets — and the line says so. A worker whose restart is
    withheld is named with its keys and why.
    """
    restart = (
        "; ".join(f"{name} (reports {', '.join(reported.keys)})" for name, reported in decision.reported.items())
        or "none"
    )
    asked = [f"{name} ({reason})" for name, reason in decision.unconfirmed.items()]
    if decision.report_unreachable:
        named = "" if decision.addressed is None else f" the reload named ({', '.join(sorted(decision.addressed))})"
        asked.append(f"every other worker{named} the roll's census shows ({UNREACHABLE_REPORT})")
    confirm = "; ".join(asked) or "none"
    withheld = _withheld_text(decision)
    writer_reason = decision.writer_reason
    if writer_reason is None:
        writer_state = "is clean"
    elif writer_reason.withheld is not None:
        writer_state = _reason_text(writer_reason)
    else:
        writer_state = f"needs its own restart ({_reason_text(writer_reason)})"
    logger.info(
        "recycle decision for %s: restart %s; confirm once ready %s; confirmed clean, left running: %s;"
        " left running, awaiting a respawn the stored config did not build for: %s; the writer %r %s",
        change,
        restart,
        confirm,
        ", ".join(sorted(decision.clean)) or "none",
        withheld,
        decision.writer.name,
        writer_state,
    )


def _withheld_text(decision: RecycleDecision) -> str:
    """The workers ``decision`` withholds a restart from, each with its keys, for a log line (``none`` when empty)."""
    return "; ".join(f"{name} (reports {', '.join(keys)})" for name, keys in decision.withheld.items()) or "none"


def _reason_text(reason: RecycleReason) -> str:
    """A restart reason in a log line: the keys a worker reported, why its state is unknown or its restart withheld."""
    if reason.unknown is not None:
        return f"state unknown: {reason.unknown}"
    if reason.withheld is not None:
        return f"reports {', '.join(reason.keys)}, its restart withheld: {reason.withheld}"
    return "reports " + ", ".join(reason.keys)


def _log_stopped_roll(
    report: RecycleReport, stopped: RecycleStop, pinned: Mapping[str, tuple[WorkerKind, int]], *, change: str
) -> None:
    """ERROR-log a recycle roll that stopped, naming the change, where (the target, or no target in hand) and why."""
    row = next((row for row in report.rows if row.name == stopped.name), None)
    reached = {row.name for row in report.rows}
    logger.error(
        "recycle for %s did not converge — stopped in the %s roll at %s (%s): %s; "
        "%d pinned target(s) not reached; the writer's own recycle is not armed",
        change,
        stopped.kind,
        repr(stopped.name) if stopped.name is not None else "no target in hand",
        row.status if row is not None else "no row",
        stopped.detail,
        len(set(pinned) - reached),
    )
