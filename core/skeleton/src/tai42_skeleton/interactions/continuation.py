"""Firing the generic resume continuation of an async ``ask`` park.

An async park stores a generic ``continuation_tool`` and the ``continuation_identity``
to run it as. When the park resolves — a human/callback ANSWER, or the expiry
reaper — the continuation fires EXACTLY ONCE, bound to the stored identity (never
the answerer's), carrying only ``{interaction_id, answer}``. The seam is
flow-blind: it runs a registered tool name under a rebound execution identity and
knows nothing of the driver's resume state.

The single-fire guarantee is the atomic answer claim (``record_answer``): both
answer doors and the reaper resolve a park through it, and only the one caller
whose claim commits fires the continuation.

Delivery is DURABLE and AT-LEAST-ONCE, via a transactional outbox. Firing is a
detached task, so a worker crash after the claim but before ``run_tool`` returns
would otherwise lose the resume forever. To close that with ZERO window, the
durable, self-contained, FLOW-BLIND continuation-due record (tool NAME +
``{interaction_id, answer}`` + stored identity + fingerprint) is enqueued in the
SAME atomic MULTI as the resolving claim (``InteractionStore.record_answer``) —
so a claimed answer can never exist without its due-record. Every resolution door
(authenticated answer, callback answer, expiry reaper) funnels through that claim,
so all three are covered by construction. ``dispatch_continuation`` then only spawns
the detached drive-and-deliver task; the drive resumes the run, DELIVERS its terminal
through the one platform ladder (``drive_and_deliver`` → ``_deliver_terminal``: fire the
run's out-of-band address, else park a waiting outcome on its subject, else drop), and
clears the record only after the terminal is delivered. A record still present past a
healthy drive's window is the reaper's signal to redeliver. Delivery is at-least-once —
a redelivery (or a slow-but-healthy drive the reaper laps) may re-drive the same run — so
every ladder rung is IDEMPOTENT, keyed by the run's per-run ``completion_id``: an address
fire dedupes, a waiting outcome writes once, and re-driving is harmless.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from typing import Any, Final
from uuid import NAMESPACE_URL, uuid5

from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    EXPIRY_ANSWER,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    InteractionRequest,
    ParkResumeFailed,
    ResumeBuffered,
    SuspendedInteraction,
    register_execution_identity_accessor,
    register_execution_identity_binder,
)
from tai42_contract.states import StateContext, SubjectCandidates
from tai42_contract.tools import RunDelivery, run_delivery
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.utils.detached_util import mark_detached_run, reset_detached_run

from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.interactions.settings import InteractionsSettings, interactions_settings
from tai42_skeleton.interactions.store import ContinuationDue, InteractionStore
from tai42_skeleton.runs.chokepoint import delivery_fire, resume_origin
from tai42_skeleton.states.context import current_state_context, state_context

# The out-of-band delivery address the platform captured on a resumed interaction: the
# ``(tool, context)`` of the door that started the run, or ``None`` for a receiver-less run.
_Delivery = tuple[str | None, Mapping[str, Any] | None] | None

logger = logging.getLogger(__name__)

# The skeleton-side surface both answer doors and the reaper resolve a park through.
# ``EXPIRY_ANSWER`` is re-exported from the contract so callers reach the fire seam
# and the expiry marker from one place.
__all__ = [
    "EXPIRY_ANSWER",
    "EXPIRY_ANSWERED_BY",
    "continuation_due_timing",
    "deliver_park_giveup",
    "dispatch_continuation",
    "drive_and_deliver",
    "fire_continuation_after_claim",
    "redeliver_continuation",
]

# Recorded as the answerer when the reaper resolves a park by expiry — a namespaced
# system sentinel that cannot collide with a looked-up user id.
EXPIRY_ANSWERED_BY: Final[str] = "system:expiry"

# Strong references to in-flight detached continuation tasks. The event loop holds
# only a WEAK reference to a bare ``create_task`` result, so without this a
# continuation could be garbage-collected mid-flight and its resume silently
# dropped. Each task removes itself in its done-callback.
_CONTINUATION_TASKS: set[asyncio.Task[Any]] = set()


def _log_continuation_done(task: asyncio.Task[Any], tool: str, interaction_id: str) -> None:
    """Surface a detached continuation's outcome loudly.

    A cancellation (shutdown) is a WARNING; any other failure — including a refused rebind of a since-disabled
    key — is an ERROR: the park resolved but its driver never resumed, which must never be swallowed.
    """
    if task.cancelled():
        logger.warning("interaction %s continuation %r was cancelled before completing", interaction_id, tool)
        return
    exc = task.exception()
    if exc is not None:
        logger.error("interaction %s continuation %r failed", interaction_id, tool, exc_info=exc)


def _completion_id(run_delivery_id: str) -> str:
    """The per-run delivery id every ladder rung keys on — a uuid5 over the run's delivery identity.

    A run has exactly one terminal, so keying on the RUN (not on whichever interaction's resume
    reached the terminal) makes every sibling / re-park / nested ask of the run resolve to the SAME
    id: an address fire dedupes, a waiting outcome writes once, a redelivery lands once.
    """
    return str(uuid5(NAMESPACE_URL, f"tai42:park-delivery:{run_delivery_id}"))


def _is_nonterminal(result: Any) -> bool:
    """Whether a resume's return is a STILL-PARKED step, not a terminal to deliver.

    A ``SuspendedInteraction`` (the run re-parked on a new ask) and a ``ResumeBuffered`` (sibling
    asks of the same super-step are still open) are both non-terminal: nothing is delivered and the
    new interaction carries the run's delivery forward.
    """
    return isinstance(result, (SuspendedInteraction, ResumeBuffered))


def _terminal_status(outcome: Any) -> str:
    """Map a driver's returned terminal to the shared completion status.

    A mapping whose ``status`` is ``"success"`` — and a bare non-mapping value, or a mapping that
    carries no status — is ``PARK_COMPLETION_SUCCEEDED``; a mapping stamped with a non-success
    ``status`` is ``PARK_COMPLETION_FAILED``. The delivery tool reads this status back to map the
    outcome to the caller (the success ``result`` vs the uniform failure notice).
    """
    if isinstance(outcome, Mapping):
        status = outcome.get("status")
        if status is not None and status != "success":
            return PARK_COMPLETION_FAILED
    return PARK_COMPLETION_SUCCEEDED


async def _clear_due(store: InteractionStore, interaction_id: str) -> None:
    """Clear the durable continuation-due record for a resolved resume.

    Opens its own client — a detached drive outlives the resolving door's connection.
    """
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        await store.clear_continuation_due(r, interaction_id)


async def _run_continuation(
    identity: str,
    fingerprint: str | None,
    tool: str,
    interaction_id: str,
    answer: Any,
    park_context: StateContext | None = None,
    park_asked_by: Sequence[str] = (),
    *,
    mark_detached: bool = True,
) -> Any:
    """Rebind ``identity`` and run ``tool`` with the park's ``{interaction_id, answer}``; RETURN its result.

    Binds ``resume_origin(interaction_id)`` UNCONDITIONALLY (the origin names the interaction being
    resumed regardless of who receives the outcome), re-deposits the park's ``StateContext``
    (so the resumed run's writes complete their provenance from the SAME door the park entered
    through), restores the parking run's call chain (passed as ``continues_chain`` so the
    continuation tool's own name is not re-added), and ALWAYS takes the ``run_tool`` return value.

    ``mark_detached`` flags the run detached (the default) so a set synchronous turn budget never
    cancels a legitimately long out-of-band resume; an inline resume under a live caller passes
    ``False`` to run under that caller's budget. The run-DELIVERY context settle (re-establishing the
    stored context, or leaving the resumer's own in place) is the caller's — :func:`drive_and_deliver`
    binds it around this drive by ``receives_outcome``.
    """
    detached_token = mark_detached_run() if mark_detached else None
    try:
        park_ctx: AbstractContextManager[Any] = (
            state_context(park_context) if park_context is not None else nullcontext()
        )
        async with bind_execution_identity(identity, bound_fingerprint=fingerprint or ""):
            with resume_origin(interaction_id), park_ctx:
                return await tai42_app.tools.run_tool(
                    tool, {"interaction_id": interaction_id, "answer": answer}, continues_chain=park_asked_by
                )
    finally:
        if detached_token is not None:
            reset_detached_run(detached_token)


async def _deliver_terminal(
    store: InteractionStore,
    *,
    interaction_id: str,
    outcome: Any,
    status: str,
    delivery: _Delivery,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None,
) -> None:
    """Deliver a resumed run's TERMINAL through the ladder — fire the address, else subject-track, else drop.

    Every rung is IDEMPOTENT, keyed by the run's ``completion_id`` (from ``run_delivery_id``): the
    address fire dedupes on it (``completion_delivery``), the waiting outcome writes once, so a
    redelivery of the same terminal lands once. An interaction reaching delivery with NO stored
    ``run_delivery_id`` RAISES loudly — a park that stored none is a platform bug, never a
    per-interaction fallback id. A transient failure of the fire propagates, so the caller keeps
    the due record for the reaper.
    """
    if run_delivery_id is None:
        raise RuntimeError(
            f"interaction {interaction_id!r} reached delivery with no stored run_delivery_id "
            "(a parkable run always mints one at its outermost start)"
        )
    completion_id = _completion_id(run_delivery_id)
    delivery_tool = delivery[0] if delivery is not None else None
    if delivery_tool is not None:
        context = dict(delivery[1]) if delivery is not None and delivery[1] is not None else {}
        payload = {**context, "result": outcome, "completion_id": completion_id, "status": status}
        # Fire the door's address under the platform's delivery-fire context so the address tool
        # passes ``assert_delivery_authorized`` — the ONE place an address is fired.
        with delivery_fire(completion_id):
            await tai42_app.tools.run_tool(delivery_tool, payload)
        return
    if candidates is not None and candidates.by_kind:
        # No address, but a subject: park the terminal for whoever next runs on the subject. Its
        # Redis TTL is a BACKSTOP at twice the retention horizon; the reaper's retention sweep drops
        # it one horizon out with a loud event, so it never leaves silently by the TTL alone.
        async with client_ctx(RedisClient, interactions_settings().redis) as r:
            await store.add_outcome(
                r,
                completion_id=completion_id,
                interaction_id=interaction_id,
                status="finished" if status == PARK_COMPLETION_SUCCEEDED else "failed",
                result=outcome,
                candidates=candidates,
                retention_ttl=2 * interactions_settings().idle_ttl_seconds,
                run_delivery_id=run_delivery_id,
            )
        return
    # Nobody waits on this run and nothing tracks it (a receiver-less run with no subject): the
    # outcome has no destination. A legitimate terminal, logged rather than delivered.
    logger.info(
        "interaction %s reached a terminal with no delivery address and no subject to track; outcome dropped",
        interaction_id,
    )


async def drive_and_deliver(
    store: InteractionStore,
    *,
    identity: str,
    fingerprint: str | None,
    tool: str,
    interaction_id: str,
    answer: Any,
    park_context: StateContext | None,
    park_asked_by: Sequence[str],
    delivery: _Delivery,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None,
    receives_outcome: bool,
) -> Any:
    """The ONE delivery chokepoint every continuation runs through — drive, then deliver its terminal.

    Settles the run-DELIVERY context by ``receives_outcome`` and drives the continuation
    (``_run_continuation``), then delivers what comes back:

    * WITHOUT a live receiver (``receives_outcome=False`` — the detached answer/expiry/reaper drive)
      it re-establishes the resumed interaction's OWN stored run-delivery context around the drive
      (``run_delivery(RunDelivery(...))``), so the run keeps its ``run_delivery_id`` and address and
      a re-park stores the same pair; WITH a live receiver (``receives_outcome=True`` — a ``visit``
      adoption) it re-establishes NOTHING, leaving the resumer's own ambient context in place.
    * a non-terminal re-park / buffered step stays parked — nothing is delivered, the due record is
      cleared, and the value is returned for a caller (``visit``) to normalise;
    * a terminal WITH a live inline receiver is RETURNED to it, nothing is fired;
    * a terminal WITHOUT a receiver runs the ladder (``_deliver_terminal``: fire the address, else
      subject-track, else drop);
    * a ``ParkResumeFailed`` is a TERMINAL to deliver FAILED — with a live receiver it clears the
      due record and propagates; without one it delivers FAILED through the ladder;
    * a PLAIN raise is keyed on ``receives_outcome``: a live receiver clears the due
      record and the error propagates to the resumer; a receiver-less drive KEEPS the record and
      surfaces the error loudly (the reaper redelivers).

    The due record is cleared only AFTER a terminal is delivered, so a transient delivery failure
    leaves it for the reaper. Returns the inline receiver's outcome (or the non-terminal value); a
    receiver-less drive returns ``None``.
    """
    run_delivery_ctx: AbstractContextManager[Any] = (
        nullcontext()
        if receives_outcome
        else run_delivery(RunDelivery(run_delivery_id, delivery) if run_delivery_id is not None else None)
    )
    park_failed: ParkResumeFailed | None = None
    try:
        with run_delivery_ctx:
            result = await _run_continuation(
                identity,
                fingerprint,
                tool,
                interaction_id,
                answer,
                park_context,
                park_asked_by,
                mark_detached=not receives_outcome,
            )
    except ParkResumeFailed as exc:
        if receives_outcome:
            # A live receiver owns the failure: clear the record and propagate it to the resumer.
            await _clear_due(store, interaction_id)
            raise
        park_failed = exc
        result = None
    except Exception:
        # A PLAIN raise: a live receiver clears the record and the error propagates; a
        # receiver-less drive KEEPS the record (never cleared here) and surfaces loudly, so the
        # reaper redelivers idempotently — the only path that does not lose a receiver-less outcome.
        if receives_outcome:
            await _clear_due(store, interaction_id)
        raise

    if park_failed is not None:
        await _deliver_terminal(
            store,
            interaction_id=interaction_id,
            outcome=park_failed.outcome,
            status=PARK_COMPLETION_FAILED,
            delivery=delivery,
            run_delivery_id=run_delivery_id,
            candidates=candidates,
        )
        await _clear_due(store, interaction_id)
        return None

    if _is_nonterminal(result):
        await _clear_due(store, interaction_id)
        return result

    if receives_outcome:
        # A live inline receiver takes the run's outcome; nothing is fired.
        await _clear_due(store, interaction_id)
        return result

    await _deliver_terminal(
        store,
        interaction_id=interaction_id,
        outcome=result,
        status=_terminal_status(result),
        delivery=delivery,
        run_delivery_id=run_delivery_id,
        candidates=candidates,
    )
    await _clear_due(store, interaction_id)
    return None


# The failed-terminal outcome the platform delivers when a resume is permanently abandoned
# (the reaper dropped its due record past the retention horizon). The delivery tool surfaces a
# FAILED status as its uniform notice and ignores this ``result``; a subject-tracked give-up stores
# it as the failure error a subject owner reads.
_RESUME_ABANDONED_OUTCOME: Final[dict[str, Any]] = {"tai42:resume_abandoned": True}


async def deliver_park_giveup(store: InteractionStore, request: InteractionRequest) -> None:
    """Deliver the run's single FAILED for a PERMANENTLY abandoned park through the platform ladder.

    The reaper dropped the continuation-due record past its retention horizon, so no redelivery will
    ever re-drive this resume. The platform reads the interaction's stored ``delivery`` and delivers
    FAILED itself — fire the run's address, else a ``failed`` waiting outcome on its subject, else
    drop — keyed by the run's ``completion_id`` so it dedupes against any FAILED already delivered.
    """
    candidates = (
        request.continuation_state_context.candidates if request.continuation_state_context is not None else None
    )
    await _deliver_terminal(
        store,
        interaction_id=request.interaction_id,
        outcome=_RESUME_ABANDONED_OUTCOME,
        status=PARK_COMPLETION_FAILED,
        delivery=request.delivery,
        run_delivery_id=request.run_delivery_id,
        candidates=candidates,
    )


def _spawn_detached_delivery(
    store: InteractionStore,
    *,
    identity: str,
    fingerprint: str | None,
    tool: str,
    interaction_id: str,
    answer: Any,
    park_context: StateContext | None,
    park_asked_by: Sequence[str],
    delivery: _Delivery,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None,
) -> None:
    """Spawn the detached drive-and-deliver task, holding a strong reference until it ends.

    The loop's own reference is weak, so an untracked task can be GC'd mid-flight and lose the
    resume; the done-callback drops the reference and surfaces the outcome loudly. Detached
    (``receives_outcome=False``): no live caller, so the ladder fires the address / subject-tracks
    / drops the terminal.
    """
    task = asyncio.create_task(
        drive_and_deliver(
            store,
            identity=identity,
            fingerprint=fingerprint,
            tool=tool,
            interaction_id=interaction_id,
            answer=answer,
            park_context=park_context,
            park_asked_by=park_asked_by,
            delivery=delivery,
            run_delivery_id=run_delivery_id,
            candidates=candidates,
            receives_outcome=False,
        ),
        name=f"interaction-continuation-{interaction_id}",
    )
    _CONTINUATION_TASKS.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        _CONTINUATION_TASKS.discard(t)
        _log_continuation_done(t, tool, interaction_id)

    task.add_done_callback(_on_done)


def continuation_due_timing(settings: InteractionsSettings) -> tuple[int, int]:
    """``(record TTL, first-attempt time in ms)`` for a durable continuation-due record, computed at call time.

    TTL = the idle retention horizon; the first
    redelivery attempt is seeded one reaper interval out, so a healthy fire clears the
    record before the reaper would ever redeliver it. Callers pass these to
    ``record_answer`` so the outbox enqueue commits in the SAME transaction as the
    claim; the caller supplies its own ``settings`` (the store holds none).
    """
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    return settings.idle_ttl_seconds, now_ms + int(settings.expiry_reaper_interval_seconds * 1000)


def dispatch_continuation(
    store: InteractionStore, request: InteractionRequest, fingerprint: str | None, answer: Any
) -> None:
    """Fire ``request``'s stored continuation ONCE, as its stored identity — a no-op for a sync question.

    Call only after a TRUE claim, which has ALREADY enqueued the
    durable continuation-due record ATOMICALLY (see ``InteractionStore.record_answer``);
    this only spawns the detached run-and-clear task. Detached so neither a slow resume
    nor a rebind refusal blocks (or 500s) the door that committed the answer; the task
    clears the durable record when ``run_tool`` returns.
    """
    if request.mode != "async" or request.continuation_tool is None:
        return
    # The model's async validator guarantees the identity is set alongside the tool.
    if request.continuation_identity is None:
        raise AssertionError
    candidates = (
        request.continuation_state_context.candidates if request.continuation_state_context is not None else None
    )
    _spawn_detached_delivery(
        store,
        identity=request.continuation_identity,
        fingerprint=fingerprint,
        tool=request.continuation_tool,
        interaction_id=request.interaction_id,
        answer=answer,
        park_context=request.continuation_state_context,
        park_asked_by=request.asked_by,
        delivery=request.delivery,
        run_delivery_id=request.run_delivery_id,
        candidates=candidates,
    )


def redeliver_continuation(store: InteractionStore, due: ContinuationDue) -> None:
    """The reaper's redelivery path: re-fire an already-persisted continuation-due record WITHOUT re-persisting.

    The retry-claim already rescheduled its next attempt. A healthy fire delivers the run's terminal
    and clears the record; a raised ``run_tool`` (or a raised delivery) leaves it for the next
    backoff window. At-least-once — the ladder's per-run ``completion_id`` dedup covers a redelivery
    that races the original fire to completion. The stored ``delivery`` address and
    ``run_delivery_id`` are re-established off the due record, so this detached redelivery binds the
    run's address and delivery identity without re-reading the request.
    """
    candidates = due.state_context.candidates if due.state_context is not None else None
    delivery: _Delivery = (due.delivery["tool"], due.delivery["context"]) if due.delivery is not None else None
    _spawn_detached_delivery(
        store,
        identity=due.identity,
        fingerprint=due.fingerprint,
        tool=due.tool,
        interaction_id=due.interaction_id,
        answer=due.answer,
        park_context=due.state_context,
        park_asked_by=due.asked_by,
        delivery=delivery,
        run_delivery_id=due.run_delivery_id,
        candidates=candidates,
    )


async def fire_continuation_after_claim(
    r: Any, store: InteractionStore, request: InteractionRequest, answer: Any
) -> None:
    """The shared post-claim seam both answer doors call after a TRUE claim.

    Reads the stashed fire fingerprint and fires the stored continuation. A sync question is a
    no-op; an async park fires its continuation (the claim already made this the sole
    resolver AND atomically enqueued the durable due-record).
    """
    if request.mode != "async" or request.continuation_tool is None:
        return
    fingerprint = await store.continuation_fingerprint(r, request.interaction_id)
    dispatch_continuation(store, request, fingerprint, answer)


def _current_state_context_for_park() -> StateContext | None:
    """The ambient state context a park records for its resume, or ``None`` when the park ran under no state context.

    The resume re-deposits the original door's subject + write provenance. Read at request-build time, inside the
    parking turn's own context.
    """
    return current_state_context()


def _current_execution_identity_for_park() -> tuple[str | None, str]:
    """Read the current execution identity as ``(user id, fingerprint)`` for a park to record.

    ``(None, "")`` when none is bound — the same two values this module captures onto the
    durable resume-continuation record (``continuation_identity`` + fingerprint). Imported
    function-locally to keep an ``authz`` module-load edge (its ``access_control.backend`` chain)
    out of this module's import.
    """
    from tai42_skeleton.authz.execution_identity import get_execution_identity

    identity = get_execution_identity()
    if identity is None or identity.user_id is None:
        return None, ""
    return identity.user_id, identity.execution_key_fingerprint or ""


def _bind_execution_identity_for_park_fire(execution_key: str, fingerprint: str) -> Any:
    """Bind ``execution_key`` (under ``fingerprint``) as the execution identity for an out-of-band park fire.

    The SAME bind the detached resume drive uses.
    """
    return bind_execution_identity(execution_key, bound_fingerprint=fingerprint)


# Wire the skeleton's execution identity into the contract's park bridge at import, so a park
# records the identity its run is authorized as and an out-of-band fire performed for it runs under
# that identity rather than fail-open. Both the capture worker and the reaper worker import this
# module at boot, so both halves are registered where they are needed.
register_execution_identity_accessor(_current_execution_identity_for_park)
register_execution_identity_binder(_bind_execution_identity_for_park_fire)
