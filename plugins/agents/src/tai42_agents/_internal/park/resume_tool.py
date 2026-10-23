"""The hidden ``agent_resume`` continuation tool, its idempotent binding, and the kill teardown.

``register_agent_resume_tool()`` binds the ``agent_resume`` driver continuation a flow-blind
platform fires when an async ``ask`` a park-capable run parked on is answered (or expires): it
carries the generic ``{interaction_id, answer, reply_to, params}``, and hands the parked run
``AskAnswer(answer, reply_to, params)`` as JSON, the value a sync ``ask`` returns. The agents
plugin's own durable park index
reverses the interaction id to its parked run and drives auto-pilot to its next outcome, which the
face returns to the platform in CONTRACT types. The face fires NO door completion — the platform
captures, binds and delivers the resumed run's outcome to the run's own address.

The face is AUTHORISED: it is dispatchable by name at the run-tool door and the MCP edge, so it
asserts ``assert_resume_authorized(interaction_id)`` at entry, before reading any park state — a
caller with no matching resume context is refused loudly.

:func:`agent_park_kill_handler` is the driver teardown the platform fires from ``kill_park`` for
every node of a killed run, and :func:`agent_park_giveup_handler` the driver epilogue the
platform's give-up fires when it abandons an answered park; both are registered ONCE at import (the
handler lists are not reset on reload), while the resume tool is (re)bound per parking-agent
registration.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.conversations import ReplyTo
from tai42_contract.interactions import AskAnswer, RunFailed
from tai42_contract.tools import TOOL_META_HIDDEN, TOOL_META_PAUSES, ToolAlreadyRegisteredError
from tai42_kit.interactions.park_giveup import ParkGiveUpOutcome, register_park_giveup_handler
from tai42_kit.interactions.park_index import DriveInProgressError, ResolutionMissingError
from tai42_kit.interactions.park_kill import ParkKillNotReadyError, register_park_kill_handler

from tai42_agents._internal.park.capability import chained_resume_from_entry
from tai42_agents._internal.park.errors import AgentResumeBarrierNotFoundError
from tai42_agents._internal.park.park_binding import agents_park_index, park_index_configured
from tai42_agents._internal.park.resume import (
    AGENT_RESUME_TOOL_NAME,
    _aborted_outcome,
    _fire_failed_terminal,
    agent_resume,
    to_contract_outcome,
)

logger = logging.getLogger(__name__)


async def agent_resume_tool(
    interaction_id: str, answer: Any, reply_to: ReplyTo | None, params: dict[str, str] | None
) -> Any:
    """Resume an async-parked agent run from an answered ``ask`` interaction, in CONTRACT types.

    This is the driver continuation the platform invokes when an async ``ask`` is answered or
    expires; it carries the generic ``{interaction_id, answer, reply_to, params}``. The parked run
    is handed ``AskAnswer(answer=answer, reply_to=reply_to, params=params)`` as JSON, so its
    waiting ``ask`` reads what a sync ``ask`` returns. AUTHORISATION: the tool is
    hidden but dispatchable by name at the run-tool door and the MCP edge, so it asserts
    ``assert_resume_authorized(interaction_id)`` FIRST, before reading any park state — a caller
    with no matching resume context (an external run-tool / MCP caller) is refused loudly with
    ``ParkResumeUnauthorizedError`` and never handed a run's outcome.

    It reverses the id to its parked run through the durable park index, buffers the answer into the
    run's super-step barrier, and — when the last sibling answer lands — drives the paused run to
    its next outcome. It fires NO door completion; the platform delivers the outcome to the run's own
    address. The return is the OUTERMOST run's outcome expressed ONLY in contract types
    (:func:`~tai42_agents._internal.park.resume.to_contract_outcome`): a ``ResumeBuffered`` while
    siblings are outstanding, a ``SuspendedInteraction`` on a re-park, the final result on a clean
    terminal, or a raised ``RunTerminalFailed`` for a failed terminal (the run's own, a failure
    returned up its chain, or a replayed failed record).

    Args:
        interaction_id: The parked interaction to resume.
        answer: The answer value (or the expiry marker) the parked run resumes with; each agent hands
            it to its paused turn its own way.
        reply_to: The message the answering reply quoted, or ``None`` (always ``None`` on expiry).
        params: The answer's channel params, or ``None`` (always ``None`` on expiry).
    """
    await tai42_app.interactions.assert_resume_authorized(interaction_id)
    answered = AskAnswer(answer=answer, reply_to=reply_to, params=params).model_dump(mode="json")
    return to_contract_outcome(await agent_resume(interaction_id, answered), raise_failed=True)


def register_agent_resume_tool() -> None:
    """Idempotently bind the hidden ``agent_resume`` continuation tool.

    Called from every parking agent's registration. The first call binds; a later call on
    the same epoch gets ``ToolAlreadyRegisteredError`` and no-ops, debug-logging it — NOT a
    process-lifetime flag (that would starve every post-boot reload epoch of its binding,
    since the module is import-cached and not re-imported on reload). Any OTHER error
    propagates loudly so a genuine registration bug is never swallowed.
    """
    try:
        tai42_app.tools.tool(
            name=AGENT_RESUME_TOOL_NAME,
            tags={"agents"},
            meta={TOOL_META_HIDDEN: True, TOOL_META_PAUSES: True},
            force=True,
        )(agent_resume_tool)
    except ToolAlreadyRegisteredError:
        logger.debug("agent_resume tool already bound this epoch; registration is a no-op")


async def _owned_entry(interaction_id: str, purpose: str) -> dict[str, Any] | None:
    """The agents' entry for ``interaction_id`` — live, resolved or detached — or ``None`` when it has none.

    ``None`` when the park index is UNCONFIGURED (this deployment loaded the agents plugin but wired
    no durable park index, so it owns no parks; the kill and give-up handlers fire for EVERY
    driver's park, so they answer "not ours" WITHOUT reading the index) and when there is NO entry
    (never parked here, or another driver owns it). A configured index that fails a read raises.
    """
    if not park_index_configured():
        logger.debug(
            "agents park %s handler: no durable park index is configured, so this driver owns no parks; "
            "interaction %r belongs to another driver",
            purpose,
            interaction_id,
        )
        return None
    return await agents_park_index().read_entry(interaction_id)


async def _live_entry(interaction_id: str, purpose: str) -> dict[str, Any] | None:
    """The agents' entry for ``interaction_id`` when it is live or resolved, or ``None`` otherwise.

    ``None`` for an entry :func:`_owned_entry` does not return and for a DETACHED tombstone (a
    chained key claimed but never parked on). A returned entry is a live entry or a resolved
    tombstone.
    """
    entry = await _owned_entry(interaction_id, purpose)
    if entry is None or agents_park_index().tombstone_kind(entry) == "detached":
        return None
    return entry


async def agent_park_kill_handler(node: str, reason: str) -> bool:
    """End the agents super-step ``node`` belongs to for a killed run; return whether this driver owns ``node``.

    A kill is a deliberate takedown of the whole run: this handler ends the super-step the node
    belongs to — a park of this run's own, or the chained call it waits on — resumes nobody and
    notifies nobody; the platform reaches every caller and every nested run of the killed run
    through this same seam. It returns whether this driver owns the node: ``False`` for an
    unconfigured index or no entry, ``True`` otherwise.

    A DETACHED tombstone (a chained key claimed but never parked on) is owned and has nothing to
    end. A RESOLVED tombstone of an ``aborted`` super-step keeps its record: a sibling's resume
    already in flight lands on it and the platform delivers nothing, and the record ages out on its
    own TTL. A delivered outcome's tombstone (terminal or suspended, or one whose record already
    aged out) is dropped with its whole super-step's tombstones, so an erase leaves no stored
    outcome (which can hold person data) behind.

    A LIVE super-step is ended under its drive lease: the kill CLAIMS it first, so it never stomps
    an in-flight resume drive — a held lease raises the kit
    :class:`~tai42_kit.interactions.park_kill.ParkKillNotReadyError`, so the platform keeps the
    kill-due record and redelivers once the drive completes or its lease expires. Holding the
    lease, it drops every OTHER resolved super-step of the run (the prior delivered outcomes, which
    can hold person data) and finalizes the super-step ``aborted``. The platform delivers the
    killed run's single DOOR terminal itself, once, after every handler returns.
    """
    entry = await _owned_entry(node, "kill")
    if entry is None:
        return False
    index = agents_park_index()
    kind = index.tombstone_kind(entry)
    if kind == "detached":
        return True
    if kind == "resolved":
        thread_id, superstep_id = index.tombstone_coordinates(entry)
        record = await index.read_resolution(thread_id, superstep_id)
        if record is None or record.resolution != "aborted":
            await index.drop_resolution(thread_id, superstep_id, member_ids=[node])
        return True

    thread_id = entry["thread_id"]
    superstep_id = entry["superstep_id"]
    lease = index.claim(thread_id, superstep_id)
    try:
        await lease.acquire()
    except DriveInProgressError as exc:
        raise ParkKillNotReadyError(
            node, f"a drive holds agent thread {thread_id!r} super-step {superstep_id!r}"
        ) from exc
    try:
        barrier = await index.read_barrier(thread_id, superstep_id)
        if barrier is None:
            raise AgentResumeBarrierNotFoundError(thread_id, superstep_id)
        logger.info("Kill of agent thread %r super-step %r (reason: %s)", thread_id, superstep_id, reason)
        await index.drop_run_resolutions(thread_id, keep=superstep_id)
        await index.finalize(
            lease, member_ids=list(barrier.expected), resolution="aborted", value=_aborted_outcome(reason)
        )
    finally:
        await lease.close()
    return True


async def agent_park_giveup_handler(interaction_id: str, failed_outcome: Mapping[str, Any]) -> ParkGiveUpOutcome | None:
    """End an agent park the platform gave up on, telling the waiter this run's driver captured.

    Fired by the platform's give-up when it drops an answered park's continuation-due record past
    the redelivery horizon (every drive of it failed before the run resumed). Not ours (an
    unconfigured index, no entry, a detached tombstone) returns ``None``. A RESOLVED tombstone (the
    drive finalized but its due record was never cleared) returns the stored outcome, as a lapped
    redelivery would replay it. A LIVE park is ended under its drive lease (a held lease raises the
    kit ``DriveInProgressError``) through the same failed-terminal epilogue a resumed run's raise
    takes, with ``RunFailed(failed_outcome)``: a chained park fires the routing its entry captured
    and returns what the waiting ancestor answered; an unchained one finalizes the failure and
    returns it.
    """
    entry = await _live_entry(interaction_id, "give-up")
    if entry is None:
        return None
    index = agents_park_index()
    if index.tombstone_kind(entry) == "resolved":
        resolved_thread, resolved_step = index.tombstone_coordinates(entry)
        record = await index.read_resolution(resolved_thread, resolved_step)
        if record is None:
            raise ResolutionMissingError(resolved_thread, resolved_step)
        return ParkGiveUpOutcome(to_contract_outcome(record.value, raise_failed=False))
    thread_id = entry["thread_id"]
    superstep_id = entry["superstep_id"]
    async with index.claim(thread_id, superstep_id) as lease:
        barrier = await index.read_barrier(thread_id, superstep_id)
        if barrier is None:
            raise AgentResumeBarrierNotFoundError(thread_id, superstep_id)
        outcome = await _fire_failed_terminal(
            lease,
            list(barrier.expected),
            RunFailed(outcome=dict(failed_outcome)),
            chained_resume_from_entry(entry),
            aborted=False,
        )
    return ParkGiveUpOutcome(to_contract_outcome(outcome, raise_failed=False))


# Registered ONCE at import. The handler lists are never reset on reload (unlike the tool
# registry), so a per-epoch re-registration would accumulate duplicate handlers; a module-import
# registration binds exactly one handler per process. The reaper worker that fires a kill or a
# give-up must load the agents plugin (import this module) for the handlers to be present — every
# worker kind that runs the reaper also loads the plugin, so they are present wherever they fire.
register_park_kill_handler(agent_park_kill_handler, continuation_tool=AGENT_RESUME_TOOL_NAME)
register_park_giveup_handler(agent_park_giveup_handler)
