"""The persist tail: locally reload through the gate, broadcast the reload to the whole fleet, and settle its recycle.

Guarantees every post-persist failure surfaces as a fleet-report-carrying error rather
than a raw exception.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence, Set
from typing import TYPE_CHECKING, Any, cast

from tai42_skeleton.app.bus import (
    FleetResult,
    LocalApplyResult,
    OpOutcome,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
)
from tai42_skeleton.app.bus.ops import ReloadConfig
from tai42_skeleton.app.discarded_build import discarded_build_statement
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.app.writer_exit import require_writer_exit_held
from tai42_skeleton.config.recycle_policy import with_own_statement
from tai42_skeleton.config.recycle_roll import (
    RollFailedError,
    RollOutcome,
    decide_and_roll,
    pinned_recycle_targets,
)
from tai42_skeleton.config.roll_handoff import RollHandOffUnknownError
from tai42_skeleton.config.service.base import _ConfigServiceBase
from tai42_skeleton.config.service.deferred_roll import (
    DeferredReload,
    DeferredRolls,
    active_deferral,
)
from tai42_skeleton.config.service.results import (
    ApplyResult,
    FleetReloadOutcome,
    joined_span_release_errors,
)
from tai42_skeleton.operations._broadcast import (
    FleetBroadcastError,
    PublishedOp,
    apply_and_publish,
    log_non_convergence,
    op_start_census,
    validate_fleet_targets,
)

if TYPE_CHECKING:
    from tai42_skeleton.app.recycle import RecycleReport

logger = logging.getLogger(__name__)


def _decision_report(report: FleetResult, writer: WorkerIdentity, local: LocalApplyResult | None) -> FleetResult:
    """The report the recycle decision reads: ``report``, with the writer's own statement kept.

    A broadcast that could not reach the bus leaves a report with no worker rows, the writer's
    own among them. The writer still knows what its own reload ``local`` answered, so when that
    entry answers for its env — an applied reload, or one refused at its epoch build
    (:func:`~tai42_skeleton.config.recycle_policy.answers_for_env`) — its row joins the report the
    decision reads, and the writer is judged from it as in a report the bus completed
    (:func:`~tai42_skeleton.config.recycle_policy.with_own_statement`). Any other failed entry,
    or none, leaves the report as it is, the writer's state unknown. ``report`` itself, which
    the reply carries, is never changed.
    """
    if report.reachable or local is None:
        return report
    return with_own_statement(report, local.as_row(writer.name))


class _BroadcastMixin(_ConfigServiceBase):
    """Post-persist reload and fleet-broadcast tail shared by the manifest/env and profile-apply pipelines.

    The tail also settles the fleet recycle, in one place for every pipeline: the census read
    once before the local apply pins the recycle targets (:meth:`_census_at_start`), and once the
    reload's report is in, :meth:`_settle_recycle` decides from it which workers need a restart
    (:func:`~tai42_skeleton.config.recycle_policy.recycle_decision`) and rolls the recycle over
    them. Inside a :func:`~tai42_skeleton.config.service.deferred_roll.deferring_rolls` scope the
    reload is recorded instead, and :meth:`run_deferred_rolls` decides and rolls once over the
    scope.
    """

    _orchestrate: Callable[..., Awaitable[RecycleReport]]

    async def _census_at_start(
        self, writer: WorkerIdentity, op_name: str, *, targets: list[str] | None
    ) -> tuple[dict[str, int] | None, dict[str, tuple[WorkerKind, int]]]:
        """Read the census once BEFORE the local apply: the reload's expected membership and the recycle pin.

        A reimporting reload is slow enough for a sibling's presence to fade across it, and the
        publish census would then never expect a worker that was live when the op began, nor would
        the roll target it. One read yields both facts: the ``{name: generation}`` membership the
        publish is judged against, and every census row of a recycle target kind but the writer
        (``name -> (kind, generation)``, gap rows included: the roll waits a target that is not
        ready back to ready before recycling it). A reload naming its ``targets`` takes no
        membership (``None``): its expected set is its target list, which the bus judges by itself.

        A census that cannot be read yields ``(None, {})`` (:func:`op_start_census`): no recycle
        target is pinned and, for a whole-fleet reload, the publish-time census stands in for the
        membership. The persist already landed, so the failed read never aborts the reload.
        """
        bus = cast("WorkerBus", self._bus)
        without = "no recycle target is pinned"
        if targets is None:
            without = (
                "the publish-time census stands in for the membership (a worker whose presence fades during the "
                f"local apply may go unreported) and {without}"
            )
        rows = await op_start_census(bus.census, op_name, without=without)
        if rows is None:
            return None, {}
        membership = bus.expected_from_census(rows) if targets is None else None
        return membership, pinned_recycle_targets(rows, writer.name)

    def _record_reload(
        self, writer: WorkerIdentity, pinned: Mapping[str, tuple[WorkerKind, int]], report: FleetResult
    ) -> bool:
        """Record the reload in the active ``deferring_rolls`` scope; ``False`` outside one."""
        deferral = active_deferral()
        if deferral is None:
            return False
        deferral.reloads.append(DeferredReload(writer=writer, pinned=dict(pinned), report=report))
        return True

    async def _settle_recycle(
        self,
        report: FleetResult,
        writer: WorkerIdentity,
        pinned: Mapping[str, tuple[WorkerKind, int]],
        *,
        local: LocalApplyResult | None,
        change: str,
        span_release_error: str | None,
    ) -> RollOutcome | None:
        """Settle the fleet recycle a persisted change's reload owes: the roll's outcome, ``None`` when it is deferred.

        The decision reads ``report`` with the writer's own entry ``local`` kept when the
        broadcast could not reach the bus (:func:`_decision_report`). Inside a ``deferring_rolls``
        scope that report is recorded there and nothing is decided (``None``). Otherwise the
        decision is read from it and the roll run (:meth:`_decide_and_roll`), the writer's own
        restart, when it needs one, left to its post-reply self-exit, armed only on a converged
        roll.
        A roll that fails — a roll a bounded run could not hand to its member among them
        (:class:`~tai42_skeleton.config.recycle_roll.RollFailedError`) — runs after the persist,
        so it surfaces as a :class:`FleetBroadcastError` carrying ``report`` with the roll's
        failure named on its error (:func:`_noting_roll_failure`) and the outcome the failed roll
        leaves as its :attr:`~FleetBroadcastError.roll`, as does the store's failed write-span
        release ``span_release_error``, which the error carries too.
        """
        decided = _decision_report(report, writer, local)
        if self._record_reload(writer, pinned, decided):
            return None
        try:
            return await self._decide_and_roll(decided, writer, pinned, change=change)
        except RollFailedError as failed:
            report = report.model_copy(update={"error": _noting_span_release(report.error, span_release_error)})
            error = FleetBroadcastError(
                report.op,
                _noting_roll_failure(report, failed.failure, change=change),
                failed.failure,
                span_release_error=span_release_error,
                local=local,
            )
            error.roll = failed.outcome
            raise error from failed.failure

    async def _settle_failed_recycle(
        self,
        error: FleetBroadcastError,
        writer: WorkerIdentity,
        pinned: Mapping[str, tuple[WorkerKind, int]],
        *,
        change: str,
    ) -> FleetBroadcastError:
        """Settle the fleet recycle a reload whose propagation failed still owes, and return ``error`` to raise.

        The decision reads ``error``'s report as it reads a successful one, with the writer's own
        entry kept when the broadcast could not reach the bus and that entry answers for its env
        (:func:`_decision_report`). A worker whose own reload was refused at its epoch build — the
        build discarded before its swap, its row stating so with the recycle-class drift standing
        after the discard (:func:`~tai42_skeleton.app.discarded_build.discarded_build_statement`) —
        serves its previous epoch under the env the discard restored: its state is known, and a
        restart it owes for that drift is withheld, since the stored config did not build on it. A
        worker whose own reload failed any other way, a worker with no row, a sibling of an
        unreachable report — each is a state unknown: a sibling is asked once it is ready and
        restarted unless it confirms clean or its reply states a refused build (its restart then
        withheld), and the writer, which cannot be asked at its own roll, needs its own restart,
        left to its post-reply self-exit and armed only on a converged roll. Either way
        ``error`` is raised. Inside a ``deferring_rolls`` scope the report is recorded for the
        scope's decision and ``error`` carries no roll; otherwise the roll runs and rides ``error``
        (:meth:`_rolled_onto`).
        """
        if self._record_reload(writer, pinned, _decision_report(error.report, writer, error.local)):
            return error
        return await self._rolled_onto(error, writer, pinned, change=change)

    async def _rolled_onto(
        self,
        error: FleetBroadcastError,
        writer: WorkerIdentity,
        pinned: Mapping[str, tuple[WorkerKind, int]],
        *,
        change: str,
        addressed: Set[str] | None = None,
    ) -> FleetBroadcastError:
        """Run the roll ``error``'s report owes and ride it on ``error``, returned to raise.

        The decision reads the report :func:`_decision_report` gives for ``error``. The roll
        (:meth:`_decide_and_roll`) rides ``error`` as its
        :attr:`~FleetBroadcastError.roll`. A roll that armed the writer's own exit
        (``self_exit_armed``) owed it into the edge's writer-exit scope, which runs it after the error
        reply. The roll is read as roll fields by the unbounded HTTP error handler's body
        (:func:`~tai42_skeleton.app.error_envelope.fleet_broadcast_error_handler`) and a backup
        import's section report; a run-tool call, an MCP tool call and a job answer with the
        raised failure's text alone. A roll that itself fails
        (:class:`~tai42_skeleton.config.recycle_roll.RollFailedError`) never hides the failure
        ``error`` raises: the roll's failure is logged at ERROR and named on ``error``'s report
        error, the outcome it leaves rides ``error`` as its roll, and ``error`` is still the one
        raised.
        """
        try:
            error.roll = await self._decide_and_roll(
                _decision_report(error.report, writer, error.local), writer, pinned, change=change, addressed=addressed
            )
        except RollFailedError as failed:
            error.roll = failed.outcome
            error.report = _noting_roll_failure(error.report, failed.failure, change=change)
        return error

    async def _decide_and_roll(
        self,
        report: FleetResult,
        writer: WorkerIdentity,
        pinned: Mapping[str, tuple[WorkerKind, int]],
        *,
        change: str,
        writer_exits: bool = True,
        addressed: Set[str] | None = None,
        holds: Sequence[str] = (),
    ) -> RollOutcome:
        """Decide from ``report`` which workers need a restart and roll over them with this pipeline's orchestrator.

        The one decide-and-roll path every reload settles through
        (:func:`~tai42_skeleton.config.recycle_roll.decide_and_roll`); ``holds`` names the roll
        holds the member carrying a handed-off roll keeps.
        """
        return await decide_and_roll(
            cast("WorkerBus", self._bus),
            report,
            writer,
            pinned,
            change=change,
            orchestrate=self._orchestrate,
            writer_exits=writer_exits,
            addressed=addressed,
            holds=holds,
        )

    async def _broadcast_profile_reload(
        self,
        expected_at_start: dict[str, int] | None,
        writer: WorkerIdentity,
        pinned: Mapping[str, tuple[WorkerKind, int]],
        *,
        swapped: bool,
        span_release_error: str | None = None,
    ) -> PublishedOp:
        """Broadcast the profile apply's reload to the whole fleet AFTER the local swap already landed.

        Returns the fleet report and this worker's own entry, which the recycle decision reads.
        The swap IS this worker's local apply, so no second local reload runs here; a process
        that ran no swap (``swapped`` false: a non-member job child) publishes with no self
        entry and its member converges as a sibling. Mirrors
        the post-persist contract: a raw broadcast failure surfaces as a
        :class:`FleetBroadcastError` carrying the honest bus-unreachable report, never a raw
        exception, so the committed env change is never hidden behind a broadcast fault; the
        recycle still settles on that report, with ``writer`` and the ``pinned`` recycle targets
        (:meth:`_settle_failed_recycle`).

        ``expected_at_start`` is the caller's PRE-SWAP membership snapshot. The swap is
        this door's local apply and it is the slowest one in the codebase (a full epoch
        rebuild), so it is exactly the window in which a sibling's presence can fade
        unnoticed — the snapshot has to be read before the swap, hence passed in rather
        than taken here. ``span_release_error`` is the store's failed release of the apply's write
        span; a raised :class:`FleetBroadcastError` carries it and names it in its report's error,
        so it is not lost behind the broadcast fault.
        """
        # The swap was this worker's reload: its result states the same fact every reload does.
        from tai42_skeleton.app.lifecycle.config_reload import awaiting_respawn

        local = (
            LocalApplyResult(
                outcome=OpOutcome.applied, payload={"status": "ok", "awaiting_respawn": awaiting_respawn()}
            )
            if swapped
            else None
        )
        try:
            report = await self._bus.publish(
                ReloadConfig(op="reload_config"), None, local, expected_at_start=expected_at_start
            )
        except Exception as broadcast_error:
            error = _noting_span_release(f"{type(broadcast_error).__name__}: {broadcast_error}", span_release_error)
            report = FleetResult(op="reload_config", reachable=False, error=error)
            raise await self._settle_failed_recycle(
                FleetBroadcastError(
                    "reload_config", report, broadcast_error, span_release_error=span_release_error, local=local
                ),
                writer,
                pinned,
                change="the profile apply",
            ) from broadcast_error
        log_non_convergence(report)
        return PublishedOp(report, local)

    async def _local_reload(
        self, bus: WorkerBus
    ) -> tuple[LocalApplyResult | None, dict[str, Any] | None, Exception | None]:
        """This process's own reload: ``(self entry, result, failure)``.

        A member reloads through the gate; a failed reload is the ``failed`` self entry and the
        failure, which the caller publishes before it re-raises. The entry's payload states a
        reload refused at its epoch build
        (:func:`~tai42_skeleton.app.discarded_build.discarded_build_statement`). A non-member (a
        forked job child) has no serving surface to converge and is never a fleet target: it reloads
        nothing and gives no self entry, its member converging as a sibling; its result states
        the store write it made.
        """
        if not bus.identity.member:
            return None, {"status": "ok", "env_keys": len(self._read_stored_env())}, None
        try:
            result = await reload_gate.run(self._admin.reload_config, reimports=True)
        except Exception as exc:
            failed = LocalApplyResult(
                outcome=OpOutcome.failed, payload=discarded_build_statement(exc), error=f"{type(exc).__name__}: {exc}"
            )
            return failed, None, exc
        return LocalApplyResult(outcome=OpOutcome.applied, payload=result), result, None

    async def _reload_and_broadcast(
        self,
        *,
        document: dict[str, Any] | None,
        continues: ApplyResult | None = None,
        span_release_error: str | None = None,
    ) -> ApplyResult:
        """Locally reload through the gate, broadcast the reload to the whole fleet, and settle its recycle.

        The census is read once before the local reload (:meth:`_census_at_start`): the publish's
        expected membership and the recycle pin. Once the broadcast returned, the reload's report
        decides which workers need a restart and the recycle rolls over them, the writer's own
        restart, when it needs one, left to its post-response self-exit, armed only on a converged
        roll (:meth:`_settle_recycle`); the result
        carries the decision, the roll's report and the verdict. Inside a
        :func:`~tai42_skeleton.config.service.deferred_roll.deferring_rolls` scope the reload is
        recorded there instead: the result reads ``roll_deferred`` and carries no decision and no
        verdict — the scope's decision judges once (:meth:`run_deferred_rolls`).

        ``continues`` is the result of an earlier run of the SAME change (the marketplace's
        post-record remount): its failed span release is the same change's and is kept on this
        result beside this run's own.

        ``span_release_error`` is the store's error when the write's exclusive span failed to
        release after the persist. The span's exit is a post-persist step too: its failure is
        recorded on the result and never raised here; a raised :class:`FleetBroadcastError`
        carries it and names it in its report's error.

        The writer is the bus member this process acts for: a process that is not itself a
        member (a job child a backend worker forked) runs no local reload — it has no serving
        surface and the bus takes no self entry from it — and publishes the reload with no local
        result; its member converges as a sibling and is the writer the decision judges from its
        row.

        Post-persist contract: this runs only after the persist has committed, and it
        guarantees EVERY subsequent failure — the local reload, the broadcast itself, OR
        the recycle roll it owes (:meth:`_settle_recycle`) — surfaces as a
        :class:`FleetBroadcastError` carrying a fleet report, never a
        raw exception. So every caller can key on one shape: a raised
        ``FleetBroadcastError`` means the change LANDED but its propagation failed
        (restore / converge), while a bare raise below this point would mean nothing
        persisted. The recycle still settles on the report of a run that raises
        (:meth:`_settle_failed_recycle`): outside a ``deferring_rolls`` scope the roll runs and
        rides the raised error; inside one the report is recorded first, so the scope's decision
        reads the latest statement of the fleet.

        A local reload that raises after the persist landed does NOT abort the
        broadcast — the siblings must still converge on the persisted state — so the
        reload publishes anyway with a ``failed`` self entry and then re-raises the
        local failure with the fleet report attached. A broadcast that raises anything
        other than the transport-unreachable shape the bus already folds into a
        returned report (e.g. a redis ``ResponseError``, or a malformed presence key
        the census cannot parse) is caught here and re-raised as a
        ``FleetBroadcastError`` carrying the honest bus-unreachable report (no worker
        list, only the error) — the same shape the bus returns for a transport
        failure — so a raw broadcast error can never escape the committed persist. An
        unconfirmed worker on the happy path is a loud ERROR log and an explicit
        report entry, but the call returns successfully.
        """
        op_name = "reload_config"
        bus = cast("WorkerBus", self._bus)
        # The writer is the member this process acts for (a forked job child writes on behalf of
        # the worker that forked it): the decision judges it from its row, the roll excludes it.
        writer = bus.member_identity
        at_start, pinned = await self._census_at_start(writer, op_name, targets=None)
        local, local_result, local_failure = await self._local_reload(bus)

        try:
            report = await self._bus.publish(ReloadConfig(op="reload_config"), None, local, expected_at_start=at_start)
        except Exception as broadcast_error:
            # The persist already committed, so a raw broadcast failure must not escape
            # the post-persist contract. Surface it as a FleetBroadcastError carrying
            # the honest bus-unreachable report (no worker list, only the error) — the
            # same shape WorkerBus.publish returns for a transport failure — so every
            # caller treats the change as landed-but-propagation-failed and restores or
            # converges rather than half-writing. A local reload that ALSO failed is
            # noted in the report error so it is not lost behind the broadcast error.
            error = f"{type(broadcast_error).__name__}: {broadcast_error}"
            if local_failure is not None:
                error = f"{error} (local reload also failed: {type(local_failure).__name__}: {local_failure})"
            report = FleetResult(op=op_name, reachable=False, error=_noting_span_release(error, span_release_error))
            raise await self._settle_failed_recycle(
                FleetBroadcastError(
                    op_name, report, broadcast_error, span_release_error=span_release_error, local=local
                ),
                writer,
                pinned,
                change="this config change",
            ) from broadcast_error

        # The report rides the response, but an unconfirmed worker is also a loud,
        # visible failure — never a silently stale sibling.
        log_non_convergence(report)
        if local_failure is not None:
            if span_release_error is not None:
                report = report.model_copy(update={"error": _noting_span_release(report.error, span_release_error)})
            raise await self._settle_failed_recycle(
                FleetBroadcastError(
                    report.op, report, local_failure, span_release_error=span_release_error, local=local
                ),
                writer,
                pinned,
                change="this config change",
            ) from local_failure
        roll = await self._settle_recycle(
            report, writer, pinned, local=local, change="this config change", span_release_error=span_release_error
        )
        if continues is not None:
            # The continued run's failed release is the same change's, so the result keeps it.
            span_release_error = joined_span_release_errors(continues.span_release_error, span_release_error)
        # local_result is set on the success path (no local_failure).
        return ApplyResult(
            fleet=report,
            local=cast("dict[str, Any]", local_result),
            document=document,
            span_release_error=span_release_error,
            roll_deferred=roll is None,
        ).rolled(roll)

    async def run_deferred_rolls(
        self, deferral: DeferredRolls, *, change: str, writer_exits: bool = True, holds: Sequence[str] = ()
    ) -> RollOutcome | None:
        """Decide and roll once over the reloads a ``deferring_rolls`` scope recorded.

        ``None`` when no run in the scope recorded a reload. Otherwise the decision reads the report
        the scope's latest run recorded (:class:`DeferredReload`) over every target the runs pinned
        (:meth:`DeferredRolls.merged`), and the roll and the verdict follow as for an inline run
        (:meth:`_decide_and_roll`): a scope whose latest run raised before its broadcast answered
        recorded a report with no sibling rows — the writer's own entry kept in it when that entry
        answers for its env (:func:`_decision_report`) — so every pinned sibling is unknown, asked
        once it is ready and restarted unless it confirms clean or its reply states a refused build
        (its restart then withheld). ``writer_exits`` says whether the writer's own restart can
        follow as its post-reply self-exit (an error reply arms it too); a caller whose reply cannot
        carry it passes ``False``, so the roll defers no self recycle and a writer still bound to a
        moved key is listed in the verdict. ``holds`` names what the caller holds across the roll as
        roll holds (:mod:`~tai42_skeleton.config.roll_holds`), which the member a handed-off roll
        goes to keeps until that roll ended. A roll that stops is logged at ERROR naming ``change``
        and returned as the partial report, as an inline roll is.
        """
        reload = deferral.merged()
        if reload is None:
            return None
        return await self._decide_and_roll(
            reload.report, reload.writer, reload.pinned, writer_exits=writer_exits, change=change, holds=holds
        )

    async def reload_fleet(self, targets: list[str] | None, *, change: str) -> FleetReloadOutcome:
        """Soft-restart the fleet from persisted config (all workers, or only ``targets``), then settle its recycle.

        ``targets`` are checked against the census before anything reloads (a name absent from it is
        a :class:`~tai42_skeleton.operations.errors.BadRequestError`). The one census read before
        the local reload pins the recycle targets and, for a whole-fleet reload, the membership the
        report is judged against. This worker reloads through the gate when it is a member the
        reload addresses (:func:`apply_and_publish`). A local reload that fails still broadcasts —
        the siblings converge on the persisted state — and the recycle below still settles on its
        report: a writer's reload refused at its epoch build leaves it serving its previous epoch
        under the env the discard restored, its state known and a restart it owes for the drift
        standing after the discard withheld, and any other failed reload of the writer is a state
        unknown; then :class:`FleetBroadcastError` is raised with the fleet report and that roll
        attached (:meth:`_rolled_onto`), the roll's own failure, when it fails, named on that
        report.

        Once the report is in, the workers the reload addressed that report a recycle-class key off
        its bound value are restarted one at a time, and the siblings whose state is unknown are
        asked once ready and restarted unless their reply confirms them clean or states a refused
        build (their restart then withheld); the writer is never asked, and when its own state is
        unknown it needs its own restart; a worker whose reload was refused at its epoch build yet
        reports keys is left running, its restart withheld and named in the verdict
        (:meth:`_decide_and_roll`; ``change`` names it in the logs); the writer's own restart, when
        it was addressed and needs one, is left to its post-reply self-exit, armed only on a
        converged roll. A broadcast that
        could not reach the bus leaves a report with no row for the writer: its own entry, applied
        or refused at its epoch build, still answers for its env and is judged as such
        (:func:`_decision_report`).
        """
        bus = cast("WorkerBus", self._bus)
        # The soft restart is this worker's own change: the edge that admitted it holds its exit
        # until it answers, so a recycle op from another worker's roll never ends it mid-reload.
        require_writer_exit_held(bus, change)
        await validate_fleet_targets(bus, targets)
        writer = bus.member_identity
        at_start, pinned = await self._census_at_start(writer, "reload_config", targets=targets)
        addressed = None if targets is None else set(targets)
        try:
            report, local = await apply_and_publish(
                bus,
                ReloadConfig(op="reload_config"),
                targets,
                lambda: reload_gate.run(self._admin.reload_config, reimports=True),
                expected_at_start=at_start,
                publish_on_local_failure=True,
                failure_payload=discarded_build_statement,
            )
        except FleetBroadcastError as error:
            await self._rolled_onto(error, writer, pinned, change=change, addressed=addressed)
            raise
        roll = await self._decide_and_roll(
            _decision_report(report, writer, local), writer, pinned, change=change, addressed=addressed
        )
        return FleetReloadOutcome(fleet=report, roll=roll)


def _noting_span_release(error: str | None, span_release_error: str | None) -> str | None:
    """A fleet report's ``error`` with the store's failed write-span release appended, when there is one."""
    if span_release_error is None:
        return error
    note = f"the store's write span also failed to release: {span_release_error}"
    return note if error is None else f"{error} ({note})"


def _noting_roll_failure(report: FleetResult, roll_error: Exception, *, change: str) -> FleetResult:
    """``report`` with the failure of the recycle roll its change owes named on its ``error``, logged at ERROR.

    The roll runs after the persist, so its failure rides the landed change's report and never
    replaces it. A roll that may have been sent to its member, whose answer is not known
    (:class:`~tai42_skeleton.config.roll_handoff.RollHandOffUnknownError`), is not judged failed:
    the note says it may have been sent and its answer is not known. A roll refused or raised is
    named failed.
    """
    failed = report.error
    cause = f"{type(roll_error).__name__}: {roll_error}"
    if isinstance(roll_error, RollHandOffUnknownError):
        state = also_state = "may have been sent to its member, and its answer is not known"
    else:
        state, also_state = "failed", "also failed"
    note = (
        f"the recycle roll the change owes {state}: {cause}"
        if failed is None
        else f"{failed} (the recycle roll the change owes {also_state}: {cause})"
    )
    logger.error("%s: %s", change, note, exc_info=roll_error)
    return report.model_copy(update={"error": note})
