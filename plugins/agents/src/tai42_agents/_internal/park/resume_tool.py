"""The hidden ``agent_resume`` continuation tool, its idempotent binding, and the kill teardown.

``register_agent_resume_tool()`` binds the ``agent_resume`` driver continuation a flow-blind
platform fires when an async ``ask`` a park-capable run parked on is answered (or expires): it
carries only the generic ``{interaction_id, answer}``. The agents plugin's own durable park index
reverses the interaction id to its parked run and drives auto-pilot to its next outcome, which the
face returns to the platform in CONTRACT types. The face fires NO door completion — the platform
captures, binds and delivers the resumed run's outcome to the run's own address.

The face is AUTHORISED: it is dispatchable by name at the run-tool door and the MCP edge, so it
asserts ``assert_resume_authorized(interaction_id)`` at entry, before reading any park state — a
caller with no matching resume context is refused loudly.

:func:`agent_park_kill_handler` is the driver teardown the platform fires from ``kill_park`` when a
parked run is killed whole-chain; it is registered ONCE at import (the handler list is not reset on
reload), while the resume tool is (re)bound per parking-agent registration.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    CHAINED_PARK_TOKEN_KEY,
    PARK_COMPLETION_FAILED,
    register_park_kill_handler,
)

from tai42_agents._internal.park.capability import chained_resume_from_entry, park_index_configured
from tai42_agents._internal.park.errors import AgentResumeBarrierNotFoundError, ParkKillNotReadyError
from tai42_agents._internal.park.index import (
    drop_run_resolution,
    finalize_resolved_superstep,
    is_resolved_tombstone,
    read_barrier,
    read_park_entry,
    read_run_resolutions,
    release_claim,
    try_claim_drive,
)
from tai42_agents._internal.park.resume import (
    AGENT_RESUME_TOOL_NAME,
    _aborted_outcome,
    agent_resume,
    encode_outcome,
    to_contract_outcome,
)

logger = logging.getLogger(__name__)


async def agent_resume_tool(interaction_id: str, answer: Any) -> Any:
    """Resume an async-parked agent run from an answered ``ask`` interaction, in CONTRACT types.

    This is the driver continuation the platform invokes when an async ``ask`` is answered or
    expires; it carries only the generic ``{interaction_id, answer}``. AUTHORISATION: the tool is
    hidden but dispatchable by name at the run-tool door and the MCP edge, so it asserts
    ``assert_resume_authorized(interaction_id)`` FIRST, before reading any park state — a caller
    with no matching resume context (an external run-tool / MCP caller) is refused loudly with
    ``ParkResumeUnauthorizedError`` and never handed a run's outcome.

    It reverses the id to its parked run through the durable park index, buffers the answer into the
    run's super-step barrier, and — when the last sibling answer lands — drives the paused graph to
    its next outcome. It fires NO door completion; the platform delivers the outcome to the run's own
    address. The return is the OUTERMOST run's outcome expressed ONLY in contract types
    (:func:`~tai42_agents._internal.park.resume.to_contract_outcome`): a ``ResumeBuffered`` while
    siblings are outstanding, a ``SuspendedInteraction`` on a re-park, the final result on a clean
    terminal, or a raised ``ParkResumeFailed`` for a mid-drive abort or an ``aborted`` replay.

    Args:
        interaction_id: The parked interaction to resume.
        answer: The answer value (or the expiry marker) fed to the awaiting tool result.
    """
    await tai42_app.interactions.assert_resume_authorized(interaction_id)
    return to_contract_outcome(await agent_resume(interaction_id, answer))


def register_agent_resume_tool() -> None:
    """Idempotently bind the hidden ``agent_resume`` continuation tool.

    Called from every parking agent's registration. The first call binds; a later call on
    the same epoch re-attempts the bind and catches the FastMCP duplicate-bind error
    (``ValueError('Component already exists: ...')``), debug-logging the no-op — NOT a
    process-lifetime flag (that would starve every post-boot reload epoch of its binding,
    since the module is import-cached and not re-imported on reload). Any OTHER error
    propagates loudly so a genuine registration bug is never swallowed.
    """
    try:
        tai42_app.tools.tool(
            name=AGENT_RESUME_TOOL_NAME,
            tags={"agents"},
            meta={"tai42/hidden": True},
            force=True,
        )(agent_resume_tool)
    except ValueError as exc:
        if "already exists" not in str(exc):
            raise
        logger.debug("agent_resume tool already bound this epoch; registration is a no-op")


async def _live_kill_entry(interaction_id: str) -> dict[str, Any] | None:
    """The LIVE park entry a kill must tear down, or ``None`` when this driver has nothing to do.

    Three cases resolve to ``None`` — the not-ours and already-resolved kills the handler no-ops on:

    * the park index is UNCONFIGURED — this deployment loaded the agents plugin but wired no durable
      park index (``TAI_AGENTS_REDIS_URL`` unset), so it owns no parks. The handler is registered
      globally and fires for EVERY driver's kill, so it answers "not ours" WITHOUT reading the index
      (an unconditional read would raise the unconfigured-redis error on a kill another driver owns);
    * NO entry — never parked here, already resumed, or its thread ended (another driver owns it);
    * a resolved TOMBSTONE — its whole super-step's resolution record and tombstones are DROPPED (the
      super-step's ids read from the run index), so a person erase reaching it per-interaction leaves
      no stored outcome (which can hold person data) behind.

    A CONFIGURED index that then fails a read still raises. A returned entry is a live parked
    super-step for the caller to tear down.
    """
    if not park_index_configured():
        logger.debug(
            "agents park-kill handler: no durable park index is configured, so this driver owns no "
            "parks; the kill of interaction %r belongs to another driver",
            interaction_id,
        )
        return None
    entry = await read_park_entry(interaction_id)
    if entry is None:
        return None
    if is_resolved_tombstone(entry):
        thread_id = entry.get("thread_id")
        superstep_id = entry.get("superstep_id")
        if thread_id is not None and superstep_id is not None:
            # Drop the WHOLE super-step's record + tombstones (its ids from the run index), so an
            # erase reaching a resolved super-step per-interaction leaves nothing of it behind.
            resolutions = await read_run_resolutions(thread_id)
            await drop_run_resolution(thread_id, superstep_id, resolutions.get(superstep_id, [interaction_id]))
        return None
    return entry


async def agent_park_kill_handler(interaction_id: str, reason: str) -> None:
    """Tear the agents plugin's own durable state for a killed interaction down, or no-op when it is not ours.

    Fired by the platform's ``kill_park`` for every killed interaction, with the run-authorization
    context bound around the fire (so the cross-driver teardown notify below authorizes on the killed
    run's shared delivery identity without any auth code of its own). :func:`_live_kill_entry`
    resolves the not-ours and already-resolved cases (an unconfigured index, no entry, a resolved
    tombstone) to a no-op; a LIVE parked super-step is torn down here:

    the kill first CLAIMS the super-step's drive lease with its own token, so it never stomps an
    in-flight resume drive — when a live drive holds the lease the claim loses and this RAISES
    ``ParkKillNotReadyError`` so the kill redelivers and lands once the drive completes or its lease
    TTL expires. Holding the lease, it fires the captured cross-driver chain FAILED so an ancestor
    that waited on it tears down too (that fire PROPAGATES on failure so the kill is redelivered and
    the ancestor is never left standing), then finalizes its super-step ``aborted`` under the same
    token — so a redrive of any still-open sibling due-record RAISES ``ParkResumeFailed``, deduped
    against the kill's own FAILED at the chain root. In the SAME teardown every OTHER resolved
    super-step of the run — the prior delivered outcomes, which can hold person data — has its record
    and tombstones DROPPED, read from the run's resolution index. A live entry whose barrier is not
    (or no longer) present RAISES so the kill redelivers.

    Only the cross-driver chain fire propagates; the local aborted-finalize of already-dead index
    state is the plugin's own teardown. The claimed lease is released on a caught teardown failure
    so the redelivery reclaims at once. The aborted outcome the killed super-step keeps is
    person-data-free (``{status, reason}``). The platform delivers the killed run's single DOOR
    FAILED itself, once, AFTER the handlers return.
    """
    entry = await _live_kill_entry(interaction_id)
    if entry is None:
        return

    thread_id = entry["thread_id"]
    superstep_id = entry["superstep_id"]
    barrier = await read_barrier(thread_id, superstep_id)
    if barrier is None:
        raise AgentResumeBarrierNotFoundError(thread_id, superstep_id)

    # Claim the super-step's drive lease before tearing it down, so a live resume drive is never
    # stomped: a lost claim means a drive holds it, so defer (redeliver) until the drive completes
    # or its lease TTL expires.
    token = str(uuid.uuid4())
    if not await try_claim_drive(thread_id, superstep_id, token):
        raise ParkKillNotReadyError(thread_id, superstep_id)

    # The run's PRIOR resolved super-steps, read before the aborted finalize adds this one.
    prior = await read_run_resolutions(thread_id)

    logger.info("Whole-chain kill of agent thread %r super-step %r (reason: %s)", thread_id, superstep_id, reason)
    aborted = _aborted_outcome(reason)
    try:
        routing = chained_resume_from_entry(entry)
        if routing is not None:
            await tai42_app.tools.run_tool(
                routing.delivery_tool,
                {CHAINED_PARK_TOKEN_KEY: routing.chain_key, "result": aborted, "status": PARK_COMPLETION_FAILED},
                continues_chain=routing.asked_by,
            )
        # Tear the killed super-step down under the held token: its park entries become aborted
        # tombstones and its barrier + drive claim are deleted (``finalize_resolved_superstep``).
        await finalize_resolved_superstep(
            thread_id,
            superstep_id,
            list(barrier["expected"]),
            resolution="aborted",
            value=encode_outcome(aborted),
            token=token,
        )
    except BaseException:
        # The finalize releases the lease on success; on a caught teardown failure release it here
        # so the redelivered kill reclaims at once rather than waiting out the lease TTL.
        await release_claim(thread_id, superstep_id, token)
        raise
    # Drop every OTHER resolved super-step of the run — its delivered outcome can hold person data.
    for prior_superstep, prior_ids in prior.items():
        if prior_superstep != superstep_id:
            await drop_run_resolution(thread_id, prior_superstep, prior_ids)


# Registered ONCE at import. The kill-handler list is never reset on reload (unlike the tool
# registry), so a per-epoch re-registration would accumulate duplicate handlers; a module-import
# registration binds exactly one handler per process. The reaper worker that fires the kill must
# load the agents plugin (import this module) for the handler to be present — every worker kind
# that runs the reaper also loads the plugin, so it is present wherever the kill fires.
register_park_kill_handler(agent_park_kill_handler)
