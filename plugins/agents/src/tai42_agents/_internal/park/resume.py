"""The ``agent_resume`` continuation the flow-blind platform fires, and the outcome conversion.

:func:`agent_resume` buffers one answer into its super-step barrier and, when the barrier is
complete, wins the single drive lease and drives the parked run to its next outcome
(:func:`_drive_completed_barrier`). At a clean terminal it cascades the OUTERMOST outcome up — if
this run was a nested run of another driver, it fires the ancestor's captured chain-delivery tool
and returns that result. It fires NO door completion: the platform captures, binds and delivers the
resumed run's outcome to the run's own address. :func:`to_contract_outcome` maps this driver's own
raw resume outcomes to the shared contract types the face returns. :data:`AGENT_RESUME_TOOL_NAME`
names the bound continuation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Final

from tai42_contract.app import tai42_app
from tai42_contract.conversations import TurnSupersededError
from tai42_contract.interactions import (
    CHAINED_PARK_TOKEN_KEY,
    PARK_COMPLETION_SUCCEEDED,
    ParkResumeFailed,
    ResumeBuffered,
    SuspendedInteraction,
    reset_chained_resume,
    set_chained_resume,
)

from tai42_agents._internal.park.capability import chained_resume_from_entry
from tai42_agents._internal.park.errors import (
    AgentResumeBarrierNotFoundError,
    AgentResumeDriveInProgressError,
    AgentResumeInterruptNotPendingError,
    AgentResumeParkEntryNotFoundError,
    AgentSuperstepLeaseLostError,
)
from tai42_agents._internal.park.index import (
    buffer_answer,
    finalize_resolved_superstep,
    heartbeat_drive_claim,
    holds_claim,
    is_resolved_tombstone,
    read_barrier,
    read_park_entry,
    read_superstep_resolution,
    release_claim,
    try_claim_drive,
)
from tai42_agents._internal.park.middleware import resuming_park_interaction_ids

logger = logging.getLogger(__name__)

# Registered name of the hidden tool that resumes an async-parked agent run. Bound as the
# driver continuation (``set_resume_continuation_tool``) so a flow-blind platform
# ``ask(async)`` stamps it onto the parked interaction and later invokes it with
# ``{interaction_id, answer}`` to resume auto-pilot. Must equal the bound tool name.
AGENT_RESUME_TOOL_NAME: Final[str] = "agent_resume"

# The tag a resolution record's stored value carries so a contract-typed outcome (a re-park
# suspended sentinel, a still-buffered partition returned up a cross-driver chain) round-trips
# the durable record and is rebuilt on a redrive; a plain terminal value rides tagged "raw".
_CONTRACT_TAG_KEY: Final[str] = "__contract__"


def is_suspended_receipt(result: Any) -> bool:
    """Whether a drive outcome is a re-park RECEIPT (the run parked again) rather than a clean terminal answer.

    The discriminator for whether the terminal cascade fires now or the outcome is carried forward
    to the new park entry as a still-suspended re-park.
    """
    return isinstance(result, dict) and result.get("status") == "suspended"


def _suspended_interaction_from_receipt(receipt: dict[str, Any]) -> SuspendedInteraction:
    """Convert this driver's own suspended-RECEIPT dict into the shared park sentinel with both id lists.

    The receipt is ``{"status": "suspended", "interaction_ids": [...], ...}`` the driver produces
    when a run parks (or re-parks). The face turns it into a :class:`SuspendedInteraction` carrying
    both id lists of the super-step — every parked interaction and the subset addressed to the
    caller — so the platform's ``visit`` normalises a whole super-step at one face. It carries NO
    ``resume_owner``: the platform re-normalises the park, it does not adopt it, and no caller may.
    """
    ids = receipt["interaction_ids"]
    caller_ids = receipt.get("caller_interaction_ids") or []
    return SuspendedInteraction(
        interaction_id=ids[0],
        expiry_at=receipt.get("expiry_at"),
        interaction_ids=list(ids),
        caller_interaction_ids=list(caller_ids),
    )


def to_contract_outcome(value: Any) -> Any:
    """Map this driver's OWN raw resume outcome to the shared contract types; pass a contract type through.

    The two driver-facing tools (:func:`~tai42_agents._internal.park.resume_tool.agent_resume_tool`
    and :func:`~tai42_agents._internal.park.chain.deliver_chained_park`) apply this to what
    :func:`agent_resume` returns, because a resume face returns the outermost run's outcome in
    CONTRACT types only — a cross-driver caller cannot read this plugin's private envelopes.

    * a ``buffered`` receipt → :class:`ResumeBuffered` naming the super-step's still-unanswered ids;
    * a ``suspended`` re-park receipt → :class:`SuspendedInteraction` with both id lists;
    * an already-contract-typed value (one that came UP a cross-driver chain fire already
      converted) → passed through unchanged;
    * anything else (a plain terminal value, ``None`` for a benign no-op landing) → returned as is.
    """
    if isinstance(value, (SuspendedInteraction, ResumeBuffered)):
        return value
    if isinstance(value, dict):
        status = value.get("status")
        if status == "suspended":
            return _suspended_interaction_from_receipt(value)
        if status == "buffered":
            return ResumeBuffered(remaining_ids=list(value.get("remaining_ids") or []))
    return value


def encode_outcome(value: Any) -> dict[str, Any]:
    """Encode a resume outcome as a JSON-safe, tagged resolution-record value.

    A contract-typed outcome (``SuspendedInteraction`` / ``ResumeBuffered`` that rode up a chain
    fire) is dumped in JSON mode under its tag so a redrive rebuilds the exact type; every other
    value rides ``raw`` and must itself be JSON-serializable (a run's terminal outcome always is,
    as every park entry already is).
    """
    if isinstance(value, SuspendedInteraction):
        return {_CONTRACT_TAG_KEY: "suspended_interaction", "data": value.model_dump(mode="json")}
    if isinstance(value, ResumeBuffered):
        return {_CONTRACT_TAG_KEY: "resume_buffered", "data": value.model_dump(mode="json")}
    return {_CONTRACT_TAG_KEY: "raw", "data": value}


def decode_outcome(encoded: Any) -> Any:
    """Rebuild an outcome from its tagged resolution-record value (:func:`encode_outcome`'s inverse)."""
    tag = encoded.get(_CONTRACT_TAG_KEY) if isinstance(encoded, dict) else None
    data = encoded["data"] if isinstance(encoded, dict) else encoded
    if tag == "suspended_interaction":
        return SuspendedInteraction.model_validate(data)
    if tag == "resume_buffered":
        return ResumeBuffered.model_validate(data)
    return data


def _aborted_outcome(reason: str) -> dict[str, str]:
    """The FAILED outcome a mid-drive abort/supersede carries on its ``ParkResumeFailed``.

    ``reason`` names the abort kind (a turn supersede, a cancellation) for the operator trace.
    """
    return {"status": "aborted", "reason": reason}


async def _replay_resolution(entry: dict[str, Any]) -> Any:
    """Replay the stored resolution a redrive lands on, or a benign no-op for a record-less tombstone.

    A resolved super-step's tombstone carries the ``(thread_id, superstep_id)`` that locate its
    resolution record. A ``terminal`` / ``suspended`` record REPLAYS its stored value (the platform
    re-runs its idempotent ladder / re-normalises the re-park); an ``aborted`` record (written by the
    kill teardown) RAISES :class:`ParkResumeFailed`, so the ladder delivers FAILED once under the
    run's delivery id, deduped against the kill's own FAILED. A record-less tombstone (a detached
    chain that never drove a super-step, or a record aged past its horizon-bounded TTL) has no
    outcome to replay, so it is the benign no-op landing.
    """
    thread_id = entry.get("thread_id")
    superstep_id = entry.get("superstep_id")
    if thread_id is None or superstep_id is None:
        return None
    record = await read_superstep_resolution(thread_id, superstep_id)
    if record is None:
        return None
    value = decode_outcome(record["value"])
    if record["resolution"] == "aborted":
        raise ParkResumeFailed(value)
    return value


async def agent_resume(interaction_id: str, answer: Any) -> Any:
    """Buffer one answer into its super-step barrier and, at the LAST of the M answers, drive it once.

    Drives the parked run to its next OUTCOME — exactly once — and returns it. This is the driver
    continuation the flow-blind platform invokes when an async ``ask`` is answered (or expires): it
    carries only ``{interaction_id, answer}``. The park index reverses the id to the parked thread,
    the super-step, and the interrupt the answers target. The answer is buffered idempotently in the
    super-step barrier; a super-step cannot advance until every interaction in it holds a resume
    value, so partial drives buy no progress and are skipped. When the barrier is complete, one
    caller wins the drive lease and feeds ALL M answers into the graph in a single
    ``Command(resume={interrupt_id: {interaction_id: answer}})``. A redelivered answer is a no-op; a
    still-pending sibling is never re-driven. An ``answer`` equal to ``EXPIRY_ANSWER`` buffers the
    same way. Single-suspend is the M=1 degenerate case.

    Returns this driver's RAW outcome — the face (:func:`to_contract_outcome`) maps it to contract
    types:

    * ``{"status": "buffered", "remaining_ids": [...]}`` while siblings are outstanding;
    * a re-park ``{"status": "suspended", ...}`` receipt when the drive parked again;
    * the OUTERMOST run's terminal outcome when this caller won the drive and drove cleanly — the
      leaf's own terminal when this run is outermost, or the return of the ancestor's captured
      chain-delivery fire (which cascades the outermost outcome UP) when it was a nested run;
    * a REPLAY of the stored resolution when the park key holds a resolved tombstone (a lapped
      redelivery of a losing sibling's orphaned due-record): the stored terminal/suspended value,
      or a raised :class:`ParkResumeFailed` for an ``aborted`` record; ``None`` for a benign
      detach tombstone.

    Raises loudly (never a bare KeyError, never a benign shape) on an interaction with no park
    entry, an interrupt the super-step does not expect or that is no longer pending, a missing
    barrier, or — when the barrier is complete but another live worker holds the drive lease —
    :class:`AgentResumeDriveInProgressError`, so the platform keeps this continuation's durable
    retry ticket for the reaper to redeliver until the live drive completes or its lease expires. A
    mid-drive ABORT or SUPERSEDE (a turn supersede / cancellation) raises
    :class:`ParkResumeFailed`, so the platform delivers FAILED and does not retry. When a
    whole-chain kill reclaimed the lease during the drive — observed by the re-check before the
    terminal chain fire, or by the token-guarded finalize — the drive fires no chain routing and
    raises :class:`AgentSuperstepLeaseLostError`, so the redrive lands on the kill's ``aborted``
    resolution instead of overwriting it. Every other raise is a PLAIN raise the platform
    retains-or-clears by the ``receives_outcome`` rule.
    """
    entry = await read_park_entry(interaction_id)
    if entry is None:
        raise AgentResumeParkEntryNotFoundError(interaction_id)
    if is_resolved_tombstone(entry):
        return await _replay_resolution(entry)

    thread_id = entry["thread_id"]
    superstep_id = entry["superstep_id"]

    try:
        present, total, remaining_ids = await buffer_answer(thread_id, superstep_id, interaction_id, answer)
    except KeyError as exc:
        raise AgentResumeInterruptNotPendingError(interaction_id, entry["interrupt_id"]) from exc

    if present < total:
        return {"status": "buffered", "remaining_ids": remaining_ids}

    token = str(uuid.uuid4())
    if not await try_claim_drive(thread_id, superstep_id, token):
        raise AgentResumeDriveInProgressError(thread_id, superstep_id)

    try:
        result, expected = await _drive_completed_barrier(entry, thread_id, superstep_id, token)
    except (TurnSupersededError, asyncio.CancelledError) as exc:
        # A mid-drive abort/supersede is a TERMINAL to deliver FAILED, not a transient retry. The
        # resume path itself writes NO resolution record for it (a kill teardown writes the
        # ``aborted`` record); it just raises, and the platform clears the due record and delivers
        # FAILED. Every OTHER raise from the drive propagates as a plain raise (the drive released
        # its lease and left the index live), so the platform retains-or-clears by receives_outcome.
        raise ParkResumeFailed(_aborted_outcome(type(exc).__name__)) from exc

    # Re-check the drive lease before the terminal chain fire. The heartbeat stopped when the drive
    # returned, so a whole-chain kill can reclaim a lapsed lease and finalize this super-step
    # ``aborted`` while the terminal cascade runs. When the lease is gone another writer owns the
    # resolution, so fire NO chain routing (a SUCCEEDED fire up to a waiting ancestor would race the
    # kill's FAILED) and raise, leaving the redrive to land on the winner's resolution.
    if not await holds_claim(thread_id, superstep_id, token):
        raise AgentSuperstepLeaseLostError(thread_id, superstep_id)

    if is_suspended_receipt(result):
        # The run parked again on a further async ask. This super-step resolves as ``suspended``,
        # carrying the re-park receipt the face re-normalises — no terminal cascade fires.
        outcome: Any = result
        resolution = "suspended"
    else:
        # A clean terminal. If this run was a NESTED run of another driver, its terminal fires the
        # ancestor's captured chain-delivery tool to re-enter the waiting ancestor, and that fire's
        # return IS the outermost run's outcome — it replaces the leaf's local result BEFORE the
        # finalize, so a redrive replays the outermost. When unchained (this run is outermost) the
        # leaf's own terminal is the outcome. Either way the driver fires NO door completion; the
        # platform delivers the outcome to the run's own address.
        routing = chained_resume_from_entry(entry)
        if routing is not None:
            result = await tai42_app.tools.run_tool(
                routing.delivery_tool,
                {CHAINED_PARK_TOKEN_KEY: routing.chain_key, "result": result, "status": PARK_COMPLETION_SUCCEEDED},
                continues_chain=routing.asked_by,
            )
        outcome = result
        resolution = "terminal"

    # Tombstone the M park entries, write this super-step's resolution record, and drop the barrier
    # + lease in ONE atomic step. A re-park wrote a fresh barrier + entries under its own super-step
    # id, so finalizing this super-step never touches the new one.
    await finalize_resolved_superstep(
        thread_id, superstep_id, list(expected), resolution=resolution, value=encode_outcome(outcome), token=token
    )
    return outcome


async def _drive_completed_barrier(
    entry: dict[str, Any],
    thread_id: str,
    superstep_id: str,
    token: str,
) -> tuple[Any, dict[str, Any]]:
    """Drive a completed super-step once, holding the drive lease.

    Reads the M buffered answers, verifies the stored interrupt is still pending, then feeds the whole
    ``{interaction_id: answer}`` map into a single resume. Returns ``(result, expected)`` — the
    drive outcome and the ``{interaction_id: expiry}`` map its caller finalizes over (after any
    terminal chain cascade). On failure releases the lease and leaves the index so a retry reclaims
    and re-drives (LangGraph preserves already-resolved RESUME writes, so a repeat drive resumes
    identically).

    The captured cross-driver chain routing is re-bound on ``chained_resume`` for the drive's
    duration, so a re-park re-persists a fresh index carrying the SAME routing forward — the
    terminal always reaches the same ancestor. An unchained run binds ``None``.

    The lease heartbeat starts BEFORE the read-barrier / compile / ``aget_state`` prefix: a
    cold compile that outruns the lease TTL must not lapse the lease while this caller still
    holds the drive, or a redelivery would reclaim and double-drive. The heartbeat is always
    stopped in the ``finally``, its own failure suppressed so it can never mask an
    already-computed drive result.
    """
    heartbeat = asyncio.create_task(heartbeat_drive_claim(thread_id, superstep_id, token))
    try:
        barrier = await read_barrier(thread_id, superstep_id)
        if barrier is None:
            await release_claim(thread_id, superstep_id, token)
            raise AgentResumeBarrierNotFoundError(thread_id, superstep_id)

        expected: dict[str, Any] = barrier["expected"]
        outputs: dict[str, Any] = barrier["outputs"]

        agent = tai42_app.agents.get_agent(entry["agent_name"])
        resume_park = getattr(agent, "aresume_park", None)
        if resume_park is None:
            await release_claim(thread_id, superstep_id, token)
            raise RuntimeError(
                f"agent {entry['agent_name']!r} bound the resume continuation but exposes no aresume_park face"
            )

        # Group the buffered answers by the interrupt each targets: every park entry stores its
        # interaction's own interrupt, so a multi-interrupt super-step (parallel subagent parks)
        # feeds each interrupt its own ``{interaction_id: answer}`` map in ONE langgraph resume.
        resume_map: dict[str, dict[str, Any]] = {}
        for interaction_id in expected:
            parked = await read_park_entry(interaction_id)
            if parked is None:
                await release_claim(thread_id, superstep_id, token)
                raise AgentResumeParkEntryNotFoundError(interaction_id)
            resume_map.setdefault(parked["interrupt_id"], {})[interaction_id] = outputs[interaction_id]

        # Re-bind the captured cross-driver chain routing for the drive's duration so a re-park
        # re-persists a fresh index carrying it forward — the terminal always reaches the same
        # ancestor. An unchained park binds ``None`` (nothing to fire on).
        chain_token = set_chained_resume(chained_resume_from_entry(entry))
        # Name the interactions being resumed so the graph's claim check adopts an in-flight park
        # whose wire marker predates the resume_owner field (a released predecessor's park) — a
        # resume is fired only for a park this run owns, so its answer is never dropped.
        try:
            with resuming_park_interaction_ids(frozenset(expected)):
                result = await resume_park(
                    rebuild_kwargs=entry["rebuild_kwargs"],
                    thread_id=thread_id,
                    resume_map=resume_map,
                )
        except BaseException:
            await release_claim(thread_id, superstep_id, token)
            raise
        finally:
            reset_chained_resume(chain_token)
    finally:
        await _stop_drive_heartbeat(heartbeat)

    return result, expected


async def _stop_drive_heartbeat(heartbeat: asyncio.Task) -> None:
    """Cancel and await the lease-heartbeat task.

    A ``CancelledError`` is the expected stop; any OTHER exception the heartbeat raised is suppressed and
    logged, never re-raised — the drive already has its result and a dying heartbeat must not overwrite it
    with a failure.
    """
    heartbeat.cancel()
    try:
        await heartbeat
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.warning(
            "Agent drive-lease heartbeat task raised; suppressed so it cannot mask the drive result",
            exc_info=True,
        )
