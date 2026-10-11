"""The persist tail: locally reload through the gate, then broadcast the reload to the whole fleet.

Guarantees every post-persist failure surfaces as a fleet-report-carrying error rather
than a raw exception.
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, cast

from tai42_skeleton.app.bus import (
    FleetResult,
    LocalApplyResult,
    OpOutcome,
    WorkerBus,
    WorkerIdentity,
    WorkerKind,
    WorkerRow,
)
from tai42_skeleton.app.recycle import RecycleError, RecycleReport, RecycleStop
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.recycle_policy import CENSUS_TARGET_KINDS, _recycle_step_timeout
from tai42_skeleton.config.service.base import _ConfigServiceBase
from tai42_skeleton.config.service.results import ApplyResult
from tai42_skeleton.operations._broadcast import (
    FleetBroadcastError,
    awaiting_respawn_after,
    log_awaiting_respawn,
    log_non_convergence,
    snapshot_membership,
)

logger = logging.getLogger(__name__)


class _BroadcastMixin(_ConfigServiceBase):
    """Post-persist reload and fleet-broadcast tail shared by the manifest/env and profile-apply pipelines.

    The tail also carries the fleet recycle roll: an env change that changes recycle-class keys
    pins its recycle targets before the local apply (:meth:`_pin_recycle_targets`) and rolls the
    recycle after the broadcast (:meth:`_roll_recycle`) — one step every env door reaches.
    """

    _orchestrate: Callable[..., Awaitable[RecycleReport]]

    async def _pin_recycle_targets(self, identity: WorkerIdentity) -> dict[str, tuple[WorkerKind, int]]:
        """The recycle targets, read from the census BEFORE the local apply can age a sibling out of it.

        A sibling whose presence faded across the slow local rebuild (or whose slot was lost and
        is re-minting) would otherwise never be targeted by the roll, and the roll would report
        converged without it.
        """
        return _pinned_recycle_targets(await cast("WorkerBus", self._bus).census(), identity.name)

    async def _roll_recycle(
        self,
        identity: WorkerIdentity,
        pinned: Mapping[str, tuple[WorkerKind, int]],
        *,
        serve_affecting: bool,
        change: str,
    ) -> RecycleReport:
        """Roll a recycle across the fleet over the ``pinned`` targets, the writer excluded.

        The writer's own recycle is deferred (``serve_affecting``) to its post-response self-exit.
        A roll that stops — on a target, or because the bus could not be read — is logged at ERROR
        naming ``change`` and returned as the partial report, never raised: the env change is
        persisted and the fleet reloaded. A raise with no recorded stop is a bus contract violation,
        not an outcome of the roll, and escapes.
        """
        try:
            return await self._orchestrate(
                self._bus,
                excluded_name=identity.name,
                applier_generation=identity.generation,
                target_kinds=CENSUS_TARGET_KINDS,
                applier_self_deferred=serve_affecting,
                step_timeout=_recycle_step_timeout(),
                expected=dict(pinned),
            )
        except RecycleError as exc:
            stopped = exc.report.stopped
            if stopped is None:
                raise
            _log_stopped_roll(exc.report, stopped, pinned, change=change)
            return exc.report

    async def _broadcast_profile_reload(self, expected_at_start: dict[str, int] | None) -> FleetResult:
        """Broadcast the profile apply's reload to the whole fleet AFTER the local swap already landed.

        The swap IS this worker's local apply, so no second local reload runs here. Mirrors
        the post-persist contract: a raw broadcast failure surfaces as a
        :class:`FleetBroadcastError` carrying the honest bus-unreachable report, never a raw
        exception, so the committed env change is never hidden behind a broadcast fault.

        ``expected_at_start`` is the caller's PRE-SWAP membership snapshot. The swap is
        this door's local apply and it is the slowest one in the codebase (a full epoch
        rebuild), so it is exactly the window in which a sibling's presence can fade
        unnoticed — the snapshot has to be read before the swap, hence passed in rather
        than taken here.
        """
        # The swap was this worker's reload: its result states the same fact every reload does.
        from tai42_skeleton.app.lifecycle.config_reload import awaiting_respawn

        local = LocalApplyResult(
            outcome=OpOutcome.applied, payload={"status": "ok", "awaiting_respawn": awaiting_respawn()}
        )
        try:
            report = await self._bus.publish({"op": "reload_config"}, None, local, expected_at_start=expected_at_start)
        except Exception as broadcast_error:
            error = f"{type(broadcast_error).__name__}: {broadcast_error}"
            report = FleetResult(op="reload_config", reachable=False, error=error)
            raise FleetBroadcastError("reload_config", report, broadcast_error) from broadcast_error
        log_non_convergence(report)
        return report

    async def _reload_and_broadcast(
        self,
        *,
        document: dict[str, Any] | None,
        recycle_keys: Sequence[str] = (),
        continues: ApplyResult | None = None,
    ) -> ApplyResult:
        """Locally reload through the gate, broadcast the reload to the whole fleet, and roll a recycle when owed.

        ``recycle_keys`` are the recycle-class keys the persisted env change carried. When there
        are any, the recycle targets are pinned beside the membership before the local reload,
        and once the broadcast returned the recycle rolls across the fleet with the writer's own
        recycle deferred to its post-response self-exit (every recycle-class change is taken as
        affecting the writer's serve surface); the result carries the roll's report.

        ``continues`` is the result of an earlier run of the SAME change whose roll already ran
        (the marketplace's post-record remount): this run pins and rolls nothing, carries that
        roll, the writer's identity and ``serve_affecting``, and judges its reload with them — one
        verdict for the change. ``recycle_keys`` and ``continues`` are exclusive. A local reload
        failure raises :class:`FleetBroadcastError` carrying ``recycle_keys`` as the change's
        unrolled keys, so a caller that recovers hands them to the reload that continues it.

        Post-persist contract: this runs only after the persist has committed, and it
        guarantees EVERY subsequent failure — the local reload OR the broadcast itself
        — surfaces as a :class:`FleetBroadcastError` carrying a fleet report, never a
        raw exception. So every caller can key on one shape: a raised
        ``FleetBroadcastError`` means the change LANDED but its propagation failed
        (restore / converge), while a bare raise below this point would mean nothing
        persisted.

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
        if recycle_keys and continues is not None:
            raise ValueError("a reload that continues an earlier run of the change rolls no recycle keys of its own")
        op_name = "reload_config"
        # Pin the expected membership BEFORE the local reload — a reimporting reload is
        # slow enough for a sibling's presence to fade across it, and the publish census
        # below would then never expect a worker that was live when the op began.
        at_start = await snapshot_membership(self._bus, op_name)
        identity: WorkerIdentity | None = None
        pinned: dict[str, tuple[WorkerKind, int]] = {}
        if recycle_keys:
            identity = cast("WorkerBus", self._bus).identity
            pinned = await self._pin_recycle_targets(identity)
        local_failure: Exception | None = None
        local_result: dict[str, Any] | None = None
        try:
            local_result = await reload_gate.run(self._admin.reload_config, reimports=True)
        except Exception as exc:
            local_failure = exc
            local = LocalApplyResult(outcome=OpOutcome.failed, error=f"{type(exc).__name__}: {exc}")
        else:
            local = LocalApplyResult(outcome=OpOutcome.applied, payload=local_result)

        try:
            report = await self._bus.publish({"op": op_name}, None, local, expected_at_start=at_start)
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
            report = FleetResult(op=op_name, reachable=False, error=error)
            raise FleetBroadcastError(op_name, report, broadcast_error) from broadcast_error

        # The report rides the response, but an unconfirmed worker is also a loud,
        # visible failure — never a silently stale sibling.
        log_non_convergence(report)
        if local_failure is not None:
            raise FleetBroadcastError(
                report.op, report, local_failure, unrolled_recycle_keys=tuple(recycle_keys)
            ) from local_failure
        recycle: RecycleReport | None = None
        if identity is not None:
            recycle = await self._roll_recycle(identity, pinned, serve_affecting=True, change="this env change")
        if continues is not None:
            recycle, identity = continues.recycle, continues.self_identity
        # local_result is set on the success path (no local_failure).
        result = ApplyResult(
            fleet=report,
            local=cast("dict[str, Any]", local_result),
            document=document,
            recycle=recycle,
            self_identity=identity,
            serve_affecting=continues.serve_affecting if continues is not None else identity is not None,
        )
        # The verdict belongs to this operation, which knows what its roll carried: the workers
        # whose reload stated a moved recycle-class env and that nothing here respawns.
        verdict = awaiting_respawn_after(report, recycle, result.self_identity, self_exit_armed=result.self_exit_armed)
        log_awaiting_respawn(verdict)
        return dataclasses.replace(result, awaiting_respawn=verdict)


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


def _pinned_recycle_targets(rows: list[WorkerRow], self_name: str) -> dict[str, tuple[WorkerKind, int]]:
    """The recycle targets pinned at the change's start, ``name -> (kind, generation)``.

    Every census row of a recycle target kind except the writer, gap rows included:
    the roll waits a target that is not ready back to ready before recycling it.
    """
    return {
        row.name: (row.kind, row.generation)
        for row in rows
        if row.kind in CENSUS_TARGET_KINDS and row.name != self_name
    }
