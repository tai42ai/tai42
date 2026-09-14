"""The persist tail — locally reload through the gate and broadcast the reload to the whole
fleet, guaranteeing every post-persist failure surfaces as a fleet-report-carrying error
rather than a raw exception."""

from __future__ import annotations

from typing import Any, cast

from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.store.store_registry import store_registry

from tai42_skeleton.app.bus import FleetResult, LocalApplyResult, OpOutcome
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.service.base import _ConfigServiceBase
from tai42_skeleton.config.service.results import ApplyResult
from tai42_skeleton.operations._broadcast import FleetBroadcastError, log_non_convergence, snapshot_membership


async def _release_llm_pools() -> None:
    """Close the loop-bound langgraph checkpoint + store resource pools so a following
    ``build_and_swap_epoch`` can reset settings: its resource-registry reset REFUSES to
    drop a per-loop registry still holding live resources on a running loop. The default
    ``release_llm_pools`` seam of :meth:`ConfigService.apply_replace_env`, mirroring
    :meth:`AppLifecycle._reload_config` (the other ``build_and_swap_epoch`` caller). Must
    run on the serving loop that owns the registries (the apply holds the reload gate on
    that loop)."""
    await checkpoint_registry().close_all()
    await store_registry().close_all()


class _BroadcastMixin(_ConfigServiceBase):
    """The post-persist reload + fleet-broadcast tail shared by the manifest/env pipeline
    and the profile-apply pipeline."""

    async def _broadcast_profile_reload(self, expected_at_start: dict[str, int] | None) -> FleetResult:
        """Broadcast the profile apply's reload to the whole fleet AFTER the local swap
        already landed (the swap IS this worker's local apply, so no second local reload
        runs here). Mirrors the post-persist contract: a raw broadcast failure surfaces as
        a :class:`FleetBroadcastError` carrying the honest bus-unreachable report, never a
        raw exception, so the committed env change is never hidden behind a broadcast
        fault.

        ``expected_at_start`` is the caller's PRE-SWAP membership snapshot. The swap is
        this door's local apply and it is the slowest one in the codebase (a full epoch
        rebuild), so it is exactly the window in which a sibling's presence can fade
        unnoticed — the snapshot has to be read before the swap, hence passed in rather
        than taken here."""
        local = LocalApplyResult(outcome=OpOutcome.applied, payload={"status": "ok"})
        try:
            report = await self._bus.publish({"op": "reload_config"}, None, local, expected_at_start=expected_at_start)
        except Exception as broadcast_error:
            error = f"{type(broadcast_error).__name__}: {broadcast_error}"
            report = FleetResult(op="reload_config", reachable=False, error=error)
            raise FleetBroadcastError("reload_config", report, broadcast_error) from broadcast_error
        log_non_convergence(report)
        return report

    async def _reload_and_broadcast(self, *, document: dict[str, Any] | None) -> ApplyResult:
        """Locally reload through the gate, then broadcast the reload to the whole
        fleet and embed the report.

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
        report entry, but the call returns successfully."""
        op_name = "reload_config"
        # Pin the expected membership BEFORE the local reload — a reimporting reload is
        # slow enough for a sibling's presence to fade across it, and the publish census
        # below would then never expect a worker that was live when the op began.
        at_start = await snapshot_membership(self._bus, op_name)
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
            raise FleetBroadcastError(report.op, report, local_failure) from local_failure
        # local_result is set on the success path (no local_failure).
        return ApplyResult(fleet=report, local=cast("dict[str, Any]", local_result), document=document)
