"""Firing the generic resume continuation of an async ``ask`` park once its answer is claimed.

When an async park resolves — a human/callback ANSWER, or the expiry reaper — its continuation
fires EXACTLY ONCE. The single-fire guarantee is the atomic answer claim (``record_answer``): both
answer doors and the reaper resolve a park through it, and only the one caller whose claim commits
fires the continuation.

Delivery is DURABLE and AT-LEAST-ONCE, via a transactional outbox. Firing is a detached task, so a
worker crash after the claim but before ``run_tool`` returns would otherwise lose the resume
forever. To close that with ZERO window, the durable, self-contained, CONSUMER-AGNOSTIC continuation-due
record (tool NAME + the answer, the quoted message's medium id and the params + stored identity +
fingerprint) is enqueued in the SAME atomic MULTI as the resolving claim
(``InteractionStore.record_answer``) — so a claimed answer can never exist without its due-record.
Every resolution door (authenticated answer, callback answer, expiry reaper) funnels through that
claim, so all three are covered by construction.
``dispatch_continuation`` then only spawns the detached drive-and-deliver task
(:func:`~tai42_skeleton.interactions.continuation.drive_and_deliver`), which clears the record only
after the run's terminal is delivered. A record still present past a healthy drive's window is the
reaper's signal to redeliver (``redeliver_continuation``).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from tai42_contract.interactions import (
    EXPIRY_ANSWER,
    CallerAskLanding,
    DeliveryAddress,
    InteractionRequest,
    InteractionResponse,
)
from tai42_contract.presets import PresetParkStages
from tai42_contract.states import StateBinding, StateContext, SubjectCandidates

from tai42_skeleton.app.root_task import spawn_root_task
from tai42_skeleton.interactions.continuation import drive_and_deliver
from tai42_skeleton.interactions.settings import InteractionsSettings
from tai42_skeleton.interactions.store import ContinuationDue, InteractionStore

logger = logging.getLogger(__name__)

# The skeleton-side surface both answer doors and the reaper resolve a park through.
# ``EXPIRY_ANSWER`` is re-exported from the contract so callers reach the fire seam
# and the expiry marker from one place.
__all__ = [
    "EXPIRY_ANSWER",
    "EXPIRY_ANSWERED_BY",
    "continuation_due_timing",
    "dispatch_continuation",
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


def _spawn_detached_delivery(
    store: InteractionStore,
    *,
    identity: str,
    fingerprint: str | None,
    tool: str,
    interaction_id: str,
    answer: Any,
    reply_to_medium_message_id: str | None,
    params: dict[str, str] | None,
    park_context: StateContext | None,
    park_asked_by: Sequence[str],
    caller_ask_landing: CallerAskLanding | None,
    chain_keys: Sequence[str],
    delivery: DeliveryAddress | None,
    run_delivery_id: str | None,
    candidates: SubjectCandidates | None,
    deferred_binding: StateBinding | None = None,
    run_input: Mapping[str, Any] | None = None,
    door_id: str | None = None,
    output_stages: PresetParkStages | None = None,
) -> None:
    """Spawn the detached drive-and-deliver task, holding a strong reference until it ends.

    The loop's own reference is weak, so an untracked task can be GC'd mid-flight and lose the
    resume; the done-callback drops the reference and surfaces the outcome loudly. Detached
    (``receives_outcome=False``): no live caller, so the ladder fires the address / subject-tracks
    / drops the terminal.
    """
    task = spawn_root_task(
        drive_and_deliver(
            store,
            identity=identity,
            fingerprint=fingerprint,
            tool=tool,
            interaction_id=interaction_id,
            answer=answer,
            reply_to_medium_message_id=reply_to_medium_message_id,
            params=params,
            park_context=park_context,
            park_asked_by=park_asked_by,
            caller_ask_landing=caller_ask_landing,
            chain_keys=chain_keys,
            delivery=delivery,
            run_delivery_id=run_delivery_id,
            candidates=candidates,
            receives_outcome=False,
            deferred_binding=deferred_binding,
            run_input=run_input,
            door_id=door_id,
            output_stages=output_stages,
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
    store: InteractionStore, request: InteractionRequest, fingerprint: str | None, response: InteractionResponse
) -> None:
    """Fire ``request``'s stored continuation ONCE with ``response``, as its stored identity.

    A no-op for a sync question.

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
        answer=response.answer,
        reply_to_medium_message_id=response.reply_to_medium_message_id,
        params=response.params,
        park_context=request.continuation_state_context,
        park_asked_by=request.asked_by,
        caller_ask_landing=request.caller_ask_landing,
        chain_keys=request.chain_keys,
        delivery=request.delivery,
        run_delivery_id=request.run_delivery_id,
        candidates=candidates,
        deferred_binding=request.deferred_binding,
        run_input=request.run_input,
        door_id=request.door_id,
        output_stages=request.deferred_output_stages,
    )


def redeliver_continuation(store: InteractionStore, due: ContinuationDue) -> None:
    """The reaper's redelivery path: re-fire an already-persisted continuation-due record WITHOUT re-persisting.

    The retry-claim already rescheduled its next attempt. A healthy fire delivers the run's terminal
    and clears the record; a raised ``run_tool`` (or a raised delivery) leaves it for the next
    backoff window. At-least-once — the ladder's per-run ``completion_id`` dedup covers a redelivery
    that races the original fire to completion. The stored ``delivery`` address and
    ``run_delivery_id`` are re-established off the due record, and its ``chain_keys`` deposited as
    the drive's resume lineage, so this detached redelivery binds the run's address, delivery
    identity and chain lineage without re-reading the request.
    """
    candidates = due.state_context.candidates if due.state_context is not None else None
    _spawn_detached_delivery(
        store,
        identity=due.identity,
        fingerprint=due.fingerprint,
        tool=due.tool,
        interaction_id=due.interaction_id,
        answer=due.answer,
        reply_to_medium_message_id=due.reply_to_medium_message_id,
        params=due.params,
        park_context=due.state_context,
        park_asked_by=due.asked_by,
        caller_ask_landing=due.caller_ask_landing,
        chain_keys=due.chain_keys,
        delivery=due.delivery,
        run_delivery_id=due.run_delivery_id,
        candidates=candidates,
        deferred_binding=due.deferred_binding,
        run_input=due.run_input,
        door_id=due.door_id,
        output_stages=due.deferred_output_stages,
    )


async def fire_continuation_after_claim(
    r: Any, store: InteractionStore, request: InteractionRequest, response: InteractionResponse
) -> None:
    """The shared post-claim seam both answer doors call after a TRUE claim of ``response``.

    Reads the stashed fire fingerprint and fires the stored continuation. A sync question is a
    no-op; an async park fires its continuation (the claim already made this the sole
    resolver AND atomically enqueued the durable due-record).
    """
    if request.mode != "async" or request.continuation_tool is None:
        return
    fingerprint = await store.continuation_fingerprint(r, request.interaction_id)
    dispatch_continuation(store, request, fingerprint, response)
