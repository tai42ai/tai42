"""The ``agent_resume`` continuation the flow-blind platform fires, and the abandonment counterpart.

:func:`agent_resume` buffers one answer into its super-step barrier and, when the barrier is
complete, wins the single drive lease and drives the parked run to completion or the next park
(:func:`_drive_completed_barrier`). :func:`fire_park_failed_completion` closes the tail a
permanently-abandoned park leaves. :data:`AGENT_RESUME_TOOL_NAME` names the bound continuation.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Mapping
from typing import Any, Final

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    bound_execution_identity_for_fire,
    reset_park_completion,
    set_park_completion,
)

from tai42_agents._internal.park.errors import (
    AgentResumeBarrierNotFoundError,
    AgentResumeDriveInProgressError,
    AgentResumeInterruptNotPendingError,
    AgentResumeParkEntryNotFoundError,
)
from tai42_agents._internal.park.index import (
    buffer_answer,
    finalize_resolved_superstep,
    heartbeat_drive_claim,
    is_resolved_tombstone,
    read_barrier,
    read_park_entry,
    release_claim,
    try_claim_drive,
)
from tai42_agents._internal.park.middleware import resuming_park_interaction_ids

logger = logging.getLogger(__name__)

# Registered name of the hidden tool that resumes an async-parked agent run. Bound as the
# driver continuation (``set_resume_continuation_tool``) so a flow-blind platform
# ``ask_user(async)`` stamps it onto the parked interaction and later invokes it with
# ``{interaction_id, answer}`` to resume auto-pilot. Must equal the bound tool name.
AGENT_RESUME_TOOL_NAME: Final[str] = "agent_resume"


def is_suspended_receipt(result: Any) -> bool:
    """Whether a drive outcome is a re-park RECEIPT (the run parked again) rather than a clean terminal answer.

    The discriminator for whether a completion fires now or is carried forward to the new park entry.
    """
    return isinstance(result, dict) and result.get("status") == "suspended"


def _completion_context(entry: dict[str, Any], thread_id: str) -> Mapping[str, Any] | None:
    """The opaque routing context a stored park entry's completion fire carries — and the re-park binding.

    The FIELD is authoritative wherever it is present (every entry this driver writes carries
    it): its value is used verbatim, an explicit ``None`` meaning the binder wired no routing
    context at all. Presence is what is tested, not truth, so an explicit ``None`` is never
    mistaken for an entry that predates the field.

    An entry carrying NO such field is a park persisted before this driver learned to store the
    context. The fallback is ``{"thread_id": ...}`` — the one address such an entry can be given,
    because it is the only one derivable from what the entry DOES carry. That rescues the case
    the address fits: an AGENT-route park, whose delivery tool routes by ``thread_id``. It is
    deliberately no wider. A field-less park taken under the TOOL-route completion binding stays
    undeliverable: that tool keys its address differently (``delivery_thread_id`` + the pinned
    route), and this driver knows no delivery tool's parameters, so nothing here can reconstruct
    an address the entry never recorded.
    """
    if "completion_context" in entry:
        context: Mapping[str, Any] | None = entry["completion_context"]
        return context
    return {"thread_id": thread_id}


def _completion_id(thread_id: str, superstep_id: str) -> str:
    """A deterministic completion-delivery id for a resolved super-step.

    So a lease-lapse re-drive fires the completion under the SAME id and the delivery ledger dedupes it to
    one record. Derived from the (thread_id, superstep_id) that uniquely name the super-step every
    redelivery re-drives.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"tai42:agent-park-completion:{thread_id}:{superstep_id}"))


async def agent_resume(interaction_id: str, answer: Any) -> Any:
    """Buffer one answer into its super-step barrier and, at the LAST of the M answers, drive it once.

    Drives the parked run to completion or the next park — exactly once.
    This is the driver continuation the flow-blind platform invokes when an async
    ``ask_user`` is answered (or expires): it carries only ``{interaction_id, answer}``.
    The park index reverses the id to the parked thread, the super-step, and the interrupt
    the answers target. The answer is buffered idempotently in the super-step barrier; a
    super-step cannot advance until every interaction in it holds a resume value, so
    partial drives buy no progress and are skipped. When the barrier is complete, one
    caller wins the drive lease and feeds ALL M answers into the graph in a single
    ``Command(resume={interrupt_id: {interaction_id: answer}})``. A redelivered answer is a
    no-op; a still-pending sibling is never re-driven. An ``answer`` equal to
    ``EXPIRY_ANSWER`` buffers the same way. Single-suspend is the M=1 degenerate case.

    Returns ``{"status": "buffered", "remaining": k}`` while siblings are outstanding, the
    driven run's outcome when this caller won the drive lease and drove, or
    ``{"status": "already_resolved"}`` when the park key holds a resolved tombstone — the
    super-step already drove cleanly and a losing sibling's orphaned due-record is being
    redelivered — so the platform clears the due-record with no alarm instead of storming on a
    key it would read as permanently dropped.

    Raises loudly (never a bare KeyError, never a benign shape) on an interaction with no
    park entry, an interrupt the super-step does not expect or that is no longer pending, a
    missing barrier, or — when the barrier is complete but another live worker holds the
    drive lease — :class:`AgentResumeDriveInProgressError`, so the platform keeps this
    continuation's durable retry ticket for the reaper to redeliver until the live drive
    completes or its lease expires.

    An ERRORED drive is RETRIED, never converted into a completion fire from this branch, and
    that is deliberate. Firing the non-success terminal on the first error would commit a delivery
    record under this super-step's STABLE completion id; the redelivery that then drove the same
    super-step to a clean success would find that id already committed and dedupe its real answer
    away — the person would be told the run failed while it in fact succeeded. So the completion
    fires from the clean-terminal branch alone, and an error leaves the index LIVE for the
    platform's at-least-once redelivery. The tail — a super-step that never drives cleanly — is
    closed at the platform's PERMANENT give-up instead: when the continuation-due record is dropped
    for good, :func:`fire_park_failed_completion` fires the non-success terminal ONCE, at the one
    point where no later success can be deduped away (the record is gone and nothing can re-drive
    the run).
    """
    entry = await read_park_entry(interaction_id)
    if entry is None:
        raise AgentResumeParkEntryNotFoundError(interaction_id)
    if is_resolved_tombstone(entry):
        return {"status": "already_resolved"}

    thread_id = entry["thread_id"]
    superstep_id = entry["superstep_id"]

    try:
        present, total = await buffer_answer(thread_id, superstep_id, interaction_id, answer)
    except KeyError as exc:
        raise AgentResumeInterruptNotPendingError(interaction_id, entry["interrupt_id"]) from exc

    if present < total:
        return {"status": "buffered", "remaining": total - present}

    token = str(uuid.uuid4())
    if not await try_claim_drive(thread_id, superstep_id, token):
        raise AgentResumeDriveInProgressError(thread_id, superstep_id)

    result, expected = await _drive_completed_barrier(entry, thread_id, superstep_id, token)
    # A completion tool was bound when this run parked (a deferred-response delivery path): on a
    # CLEAN TERMINAL drive AWAIT the completion handoff with the final answer BEFORE finalizing
    # the index. The handoff's durable point is the delivery record commit, deduped by the
    # stable completion id, so a crash before it leaves the index LIVE for an idempotent
    # redelivery to re-drive and re-reach the handoff, and a crash after it lands on the
    # tombstone. A re-park carried the tool forward onto the new entry, so it is NOT fired here.
    # Subscript, not ``get``: every persisted park entry writes this field, so an entry without
    # it is a corrupt index and belongs raised, not silently read as "no completion wired". The
    # rebind below reads it the same way.
    completion_tool = entry["completion_tool"]
    if completion_tool is not None and not is_suspended_receipt(result):
        await tai42_app.tools.run_tool(
            completion_tool,
            {
                # The generic completion-fire payload: the binder's OPAQUE context merged with
                # this terminal's outcome. The context carries whatever the delivery tool needs
                # to route the answer, so no delivery tool's parameters are named here.
                **(_completion_context(entry, thread_id) or {}),
                "result": result,
                "completion_id": _completion_id(thread_id, superstep_id),
                # Reaching here IS the clean-terminal branch: a failed drive raised and a re-park
                # is excluded above, so the outcome is a success.
                "status": PARK_COMPLETION_SUCCEEDED,
            },
        )
    # Tombstone the M park entries and drop the barrier + lease in ONE atomic step, so a crash
    # can never leave a partial tombstone set. A re-park wrote a fresh barrier + entries under
    # its own super-step id, so finalizing this super-step never touches the new one.
    await finalize_resolved_superstep(thread_id, superstep_id, list(expected))
    return result


async def fire_park_failed_completion(interaction_id: str) -> None:
    """Fire the bound completion with the FAILED terminal for a park whose resume was PERMANENTLY abandoned.

    Closes the tail :func:`agent_resume` cannot close from its error branch.
    Registered (:func:`~tai42_agents._internal.park.resume_tool.register_agent_resume_tool`) as the
    continuation-abandonment handler the platform's redelivery reaper fires by interaction id when a
    park's durable continuation-due record is dropped for good. A park that never drove cleanly
    would otherwise leave the bound caller (a conversation turn, a chained park) waiting to its own
    deadline; this delivers the non-success terminal so it learns the answer is never coming.

    Delivery here is AT-MOST-ONCE, unlike the at-least-once success path: it is a fire-and-forget
    once the durable record is gone, so a transient failure of THIS fire loses the notice with no
    retry behind it — strictly better than the silent tail-drop it replaces, and named so a reader
    does not expect the success path's durability.

    This does NOT drive: the record is gone and no answer will ever arrive, so there is nothing to
    resume. It reads the park entry, and:

    * a MISSING entry (its own TTL lapsed) has nothing left to deliver against — a no-op;
    * a RESOLVED tombstone means the super-step already drove to a clean terminal — a no-op, so a
      FAILED fire never overrides a delivered SUCCESS;
    * a run-face park (``completion_tool is None``) has no out-of-band delivery path — a no-op;
    * a LIVE drive still holding the super-step's lease may yet deliver SUCCESS — the drive-claim
      attempt below fails, so the FAILED fire is skipped rather than racing it. A clean drive drops
      that lease and writes its tombstone in ONE atomic finalize, so a released lease always implies
      the tombstone above: the claim guard and the tombstone guard between them leave no window
      where a SUCCESS is still coming. The stable completion id is the final backstop under the
      consumer's dedupe for any interleaving beyond those — a property this terminus accepts.

    When it fires, it holds the drive lease for the span and binds the park's recorded execution
    identity (:func:`~tai42_contract.interactions.bound_execution_identity_for_fire`), so the
    completion — and, for a chained park, the outer ``agent_resume`` drive it triggers — runs
    AUTHORIZED as the run did, never fail-open. A park persisted before the identity was recorded
    carries no key: the fire runs UNBOUND with a warning naming the limitation, the pre-upgrade
    behavior, never a crash. The fire uses the generic contract payload under
    :data:`~tai42_contract.interactions.PARK_COMPLETION_FAILED`, keyed by the SAME stable
    ``completion_id`` a clean terminal would use, so the delivery ledger deduping on that id
    collapses a redundant sibling fire to one record. It does not tombstone; the park entries
    expire on their own once abandoned, and repeated abandonment is idempotent through that
    completion-id dedupe.

    Subscript, not ``get``, for the always-written fields (``completion_tool``/``thread_id``/
    ``superstep_id``): every persisted park entry writes them, so an entry missing one is a corrupt
    index and belongs raised (the caller's abandonment fire is guarded and logs it), not silently
    read as 'nothing to deliver'. The identity fields use ``get``: an entry without a recorded
    identity fires its abandonment completion UNBOUND (authz skipped), distinct from a corrupt
    entry missing a structural field.
    """
    entry = await read_park_entry(interaction_id)
    if entry is None or is_resolved_tombstone(entry):
        return
    completion_tool = entry["completion_tool"]
    if completion_tool is None:
        return
    thread_id = entry["thread_id"]
    superstep_id = entry["superstep_id"]

    # Take the super-step's drive lease: a live drive holding it may still deliver SUCCESS, so
    # failing to claim means skip the FAILED fire rather than race it. Held across the fire so no
    # drive can start under us, released after so nothing leaks (there is no retry to reclaim for).
    token = str(uuid.uuid4())
    if not await try_claim_drive(thread_id, superstep_id, token):
        return
    try:
        async with bound_execution_identity_for_fire(
            entry.get("execution_identity"), entry.get("execution_fingerprint") or ""
        ):
            await tai42_app.tools.run_tool(
                completion_tool,
                {
                    **(_completion_context(entry, thread_id) or {}),
                    "result": None,
                    "completion_id": _completion_id(thread_id, superstep_id),
                    "status": PARK_COMPLETION_FAILED,
                },
            )
    finally:
        await release_claim(thread_id, superstep_id, token)


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
    clean-terminal completion handoff). On failure releases the lease and leaves the index so a
    retry reclaims and re-drives (LangGraph preserves already-resolved RESUME writes, so a
    repeat drive resumes identically).

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

        # Bind the stored completion tool AND its opaque context for the drive's duration so a
        # re-park re-persists a fresh index carrying both forward — the deferred-response
        # delivery survives every re-park with its routing address intact. The entry always
        # carries the ``completion_tool`` field (written on every persisted park entry). The
        # context reads through the SAME field-less fallback the fire uses, so an entry written
        # before the field existed re-parks with its address rather than rebinding an empty one
        # and dropping the answer a re-park later. No tool (the run face, whose caller receives
        # the resumed result directly) binds NOTHING: a context with no tool to fire addresses
        # a delivery that will never happen, and the binding is one atomic pair.
        completion_tool = entry["completion_tool"]
        completion_token = set_park_completion(
            completion_tool, _completion_context(entry, thread_id) if completion_tool is not None else None
        )
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
            reset_park_completion(completion_token)
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
