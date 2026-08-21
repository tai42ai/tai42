"""Firing the generic resume continuation of an async ``ask_user`` park.

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
so all three are covered by construction. ``dispatch_continuation`` then only FIRES
the detached run-and-clear task; the fire clears the record when ``run_tool``
returns. A record still present past a healthy fire's window is the expiry reaper's
signal to redeliver. Delivery is at-least-once — a redelivery (or a slow-but-healthy
fire the reaper laps) may fire the same continuation more than once — so the
CONSUMER's resume must be idempotent: applying the same resume twice must be harmless.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final

from tai42_contract.app import tai42_app
from tai42_contract.interactions import EXPIRY_ANSWER, InteractionRequest
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.authz.execution import bind_execution_identity
from tai42_skeleton.interactions.settings import InteractionsSettings, interactions_settings
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
    """Surface a detached continuation's outcome loudly. A cancellation (shutdown)
    is a WARNING; any other failure — including a refused rebind of a since-disabled
    key — is an ERROR: the park resolved but its driver never resumed, which must
    never be swallowed."""
    if task.cancelled():
        logger.warning("interaction %s continuation %r was cancelled before completing", interaction_id, tool)
        return
    exc = task.exception()
    if exc is not None:
        logger.error("interaction %s continuation %r failed", interaction_id, tool, exc_info=exc)


async def _run_continuation(
    identity: str, fingerprint: str | None, tool: str, interaction_id: str, answer: Any
) -> None:
    """Rebind ``identity`` (under the captured ``fingerprint``) and run ``tool`` with
    the park's ``{interaction_id, answer}``. Runs in a detached task so a long resume
    never blocks the resolving door; the bind is refused loudly if the key can no
    longer carry the fire."""
    async with bind_execution_identity(identity, bound_fingerprint=fingerprint or ""):
        await tai42_app.tools.run_tool(tool, {"interaction_id": interaction_id, "answer": answer})


async def _run_and_clear(
    store: InteractionStore, identity: str, fingerprint: str | None, tool: str, interaction_id: str, answer: Any
) -> None:
    """Run the continuation, then clear its durable due-record — the at-least-once
    delivery body. ``run_tool`` returning means the consumer durably applied the
    resume, so the record is cleared. If ``run_tool`` RAISES (e.g. its target
    state was not yet persisted — an ordering race — or any transient fault),
    the record is deliberately NOT cleared: the exception propagates to the task's
    done-callback (logged loudly, never swallowed) and the record stays for the reaper
    to redeliver on backoff. The clear opens its own client: the detached task outlives
    the resolving door's connection."""
    await _run_continuation(identity, fingerprint, tool, interaction_id, answer)
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        await store.clear_continuation_due(r, interaction_id)


def _spawn_continuation_task(
    store: InteractionStore, identity: str, fingerprint: str | None, tool: str, interaction_id: str, answer: Any
) -> None:
    """Spawn the detached run-and-clear task, holding a strong reference until it ends.
    The loop's own reference is weak, so an untracked task can be GC'd mid-flight and
    lose the resume; the done-callback drops the reference and surfaces the outcome
    loudly."""
    task = asyncio.create_task(
        _run_and_clear(store, identity, fingerprint, tool, interaction_id, answer),
        name=f"interaction-continuation-{interaction_id}",
    )
    _CONTINUATION_TASKS.add(task)

    def _on_done(t: asyncio.Task[Any]) -> None:
        _CONTINUATION_TASKS.discard(t)
        _log_continuation_done(t, tool, interaction_id)

    task.add_done_callback(_on_done)


def continuation_due_timing(settings: InteractionsSettings) -> tuple[int, int]:
    """``(record TTL, first-attempt time in ms)`` for a durable continuation-due
    record, computed at call time. TTL = the idle retention horizon; the first
    redelivery attempt is seeded one reaper interval out, so a healthy fire clears the
    record before the reaper would ever redeliver it. Callers pass these to
    ``record_answer`` so the outbox enqueue commits in the SAME transaction as the
    claim; the caller supplies its own ``settings`` (the store holds none)."""
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    return settings.idle_ttl_seconds, now_ms + int(settings.expiry_reaper_interval_seconds * 1000)


def dispatch_continuation(
    store: InteractionStore, request: InteractionRequest, fingerprint: str | None, answer: Any
) -> None:
    """Fire ``request``'s stored continuation ONCE, as its stored identity — a no-op
    for a sync question. Call only after a TRUE claim, which has ALREADY enqueued the
    durable continuation-due record ATOMICALLY (see ``InteractionStore.record_answer``);
    this only spawns the detached run-and-clear task. Detached so neither a slow resume
    nor a rebind refusal blocks (or 500s) the door that committed the answer; the task
    clears the durable record when ``run_tool`` returns."""
    if request.mode != "async" or request.continuation_tool is None:
        return
    # The model's async validator guarantees the identity is set alongside the tool.
    assert request.continuation_identity is not None
    _spawn_continuation_task(
        store,
        request.continuation_identity,
        fingerprint,
        request.continuation_tool,
        request.interaction_id,
        answer,
    )


def redeliver_continuation(store: InteractionStore, due: ContinuationDue) -> None:
    """The reaper's redelivery path: re-fire an already-persisted continuation-due
    record WITHOUT re-persisting (the retry-claim already rescheduled its next
    attempt). A healthy fire clears the record; a raised ``run_tool`` leaves it for the
    next backoff window. At-least-once — the consumer's idempotency covers a redelivery
    that races the original fire to completion."""
    _spawn_continuation_task(store, due.identity, due.fingerprint, due.tool, due.interaction_id, due.answer)


async def fire_continuation_after_claim(
    r: Any, store: InteractionStore, request: InteractionRequest, answer: Any
) -> None:
    """The shared post-claim seam both answer doors call after a TRUE claim: read the
    stashed fire fingerprint and fire the stored continuation. A sync question is a
    no-op; an async park fires its continuation (the claim already made this the sole
    resolver AND atomically enqueued the durable due-record)."""
    if request.mode != "async" or request.continuation_tool is None:
        return
    fingerprint = await store.continuation_fingerprint(r, request.interaction_id)
    dispatch_continuation(store, request, fingerprint, answer)
