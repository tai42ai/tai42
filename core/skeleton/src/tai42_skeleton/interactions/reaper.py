"""The async-park expiry reaper.

An async ``ask_user`` returns immediately with no blocking waiter, so nothing else
fires its expiry. This periodic loop scans the per-interaction expiry index and,
for each async park whose ``expiry_at`` has passed and that is still unanswered,
claims it ONCE — through the same atomic ``record_answer`` a human answer takes,
so an answer racing an expiry (or two reaper workers racing) resolves to exactly
one — and fires the stored continuation with the generic EXPIRY answer. The claim
drops the park from the expiry index; a member whose state already vanished or was
answered is reconciled off the index without firing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from redis.asyncio import Redis
from tai42_contract.interactions import InteractionRequest, InteractionResponse
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.continuation import (
    EXPIRY_ANSWER,
    EXPIRY_ANSWERED_BY,
    continuation_due_timing,
    dispatch_continuation,
    redeliver_continuation,
)
from tai42_skeleton.interactions.settings import (
    InteractionsSettings,
    interactions_settings,
    interactions_store_configured,
)
from tai42_skeleton.interactions.store import CONTINUATION_DROPPED, InteractionStore

logger = logging.getLogger(__name__)

# The platform-event topic emitted when a parked ask expires UNANSWERED — the reaper
# has just claimed it by the EXPIRY sentinel. Core states the fact; a deployment wires a
# hook on this topic in config to decide what an operator sees. It RIDES ALONGSIDE the
# expiry continuation the reaper fires — it never perturbs the claim/continuation flow.
ASK_EXPIRED_UNANSWERED_EVENT_TOPIC = "interactions_ask_expired_unanswered"


async def _emit_ask_expired_unanswered(request: InteractionRequest, group_id: str, expired_at: datetime) -> None:
    """Emit the ``interactions_ask_expired_unanswered`` platform event ONCE for a park
    the reaper has just claimed by expiry.

    Core states the fact; a deployment wires a hook on this topic to decide what an
    operator sees. Best-effort: a hooks-manager failure is logged and swallowed so it
    never perturbs the reaper's claim/continuation flow or aborts the pass — the expiry
    continuation has already been dispatched when this runs."""
    # Local import: reach the hooks-manager accessor only when emitting, mirroring the
    # inbound ladder's pattern (keeps a module-load import edge out of this module).
    from tai42_skeleton.hooks.cache import get_hooks_manager

    payload = {
        "interaction_id": request.interaction_id,
        "group_id": group_id,
        "channel": request.channel,
        "recipient": request.recipient,
        "expired_at": expired_at.isoformat(),
    }
    try:
        await get_hooks_manager().on_event(topic=ASK_EXPIRED_UNANSWERED_EVENT_TOPIC, payload=payload)
    except Exception:
        logger.warning(
            "interactions expiry reaper: failed to emit %r for expired park %s",
            ASK_EXPIRED_UNANSWERED_EVENT_TOPIC,
            request.interaction_id,
            exc_info=True,
        )


async def reap_expired_parks_once() -> int:
    """One reaper pass: resolve every due async park by expiry, returning how many
    continuations this pass fired. A no-op (returns 0) when the interactions store is
    unconfigured — the feature is OFF and no park can exist."""
    if not interactions_store_configured():
        return 0
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    fired = 0
    async with client_ctx(RedisClient, settings.redis) as r:
        now = datetime.now(UTC)
        for interaction_id in await store.due_expiries(r, now):
            # Guard each member: one park that deterministically raises must not abort
            # the whole pass every interval and starve the others. Log loudly, continue.
            try:
                if await _reap_one_expired_park(r, store, settings, interaction_id, now):
                    fired += 1
            except Exception:
                logger.error(
                    "interactions expiry reaper failed on park %s; skipping it this pass",
                    interaction_id,
                    exc_info=True,
                )
    return fired


async def _reap_one_expired_park(
    r: Redis, store: InteractionStore, settings: InteractionsSettings, interaction_id: str, now: datetime
) -> bool:
    """Resolve a single due park by expiry, returning True when this call fired its
    continuation. Vanished/answered/boundary members are reconciled or skipped."""
    state = await store.get_state(r, interaction_id)
    if state is None or state.status != "pending" or state.request.mode != "async" or state.request.expiry_at is None:
        # Vanished, already answered, or no longer an async park with a
        # deadline: drop the stale index member and move on.
        await store.drop_expiry_member(r, interaction_id)
        return False
    if now < state.request.expiry_at:
        # Indexed but not yet due (a boundary member); leave it for a later pass.
        return False
    fingerprint = await store.continuation_fingerprint(r, interaction_id)
    response = InteractionResponse(
        interaction_id=interaction_id,
        answer=EXPIRY_ANSWER,
        answered_by=EXPIRY_ANSWERED_BY,
        answered_at=now,
    )
    # A late-answer reply key ≈ the remaining park budget; past expiry it
    # floors to 1s. The claim also drops the expiry-index member and, ATOMICALLY,
    # enqueues the durable continuation-due record.
    reply_ttl = max(1, int((state.request.timeout_at - now).total_seconds()))
    due_ttl, due_first_attempt_at_ms = continuation_due_timing(settings)
    claimed = await store.record_answer(
        r,
        response,
        state.group_id,
        reply_ttl,
        continuation_due_ttl=due_ttl,
        continuation_first_attempt_at_ms=due_first_attempt_at_ms,
    )
    if claimed:
        # Exactly-once: only the claim that committed fires. A human answer
        # (or a sibling reaper) that claimed first returns False here and its
        # own door fired the continuation.
        dispatch_continuation(store, state.request, fingerprint, EXPIRY_ANSWER)
        # State the expiry as a best-effort platform event AFTER the continuation is
        # dispatched, so a hooks failure (swallowed inside) can never come between the
        # claim and the continuation fire. Rides alongside; never perturbs the flow.
        await _emit_ask_expired_unanswered(state.request, state.group_id, now)
        return True
    return False


async def redeliver_due_continuations_once() -> int:
    """One redelivery pass: re-fire every continuation-due record whose next-attempt
    time has passed, returning how many this pass redelivered. These are resolves that
    were durably recorded but whose ``run_tool`` never returned — a worker crash after
    the claim, or an ordering race in which the resume tool raised because its target
    state was not yet persisted (so its record was never cleared). Each
    redelivery advances the record's backoff atomically before firing, so concurrent
    passes don't storm the same record; a healthy fire clears it. A no-op (returns 0)
    when the interactions store is unconfigured."""
    if not interactions_store_configured():
        return 0
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    redelivered = 0
    async with client_ctx(RedisClient, settings.redis) as r:
        now = datetime.now(UTC)
        backoff_base_ms = int(settings.expiry_reaper_interval_seconds * 1000)
        # Cap the backoff at the record's own TTL — retrying past its lifetime is
        # pointless (the record would have TTL-expired and been reconciled off).
        backoff_cap_ms = settings.idle_ttl_seconds * 1000
        for interaction_id in await store.due_continuations(r, now):
            # Guard each member: one poison record must not abort the whole pass and
            # starve the rest. Log loudly, continue.
            try:
                due = await store.claim_continuation_retry(r, interaction_id, now, backoff_base_ms, backoff_cap_ms)
                if due is None:
                    # Not (or no longer) due: cleared or re-claimed this window by a
                    # racing pass. Nothing to fire.
                    continue
                if due is CONTINUATION_DROPPED:
                    # The record TTL-expired past its retention horizon and was
                    # reconciled off the index: a PERMANENT give-up — no redelivery will
                    # ever fire this resume. Surface it loudly, once, at the terminus.
                    logger.error(
                        "resume for interaction %s permanently dropped after exhausting "
                        "its retention horizon; its continuation will never fire",
                        interaction_id,
                    )
                    continue
                redeliver_continuation(store, due)
                redelivered += 1
            except Exception:
                logger.error(
                    "interactions reaper failed redelivering continuation %s; skipping it this pass",
                    interaction_id,
                    exc_info=True,
                )
    return redelivered


async def run_expiry_reaper_loop() -> None:
    """Run the expiry reaper until cancelled.

    Each pass sleeps the configured interval, then (1) resolves every due async park by
    expiry and (2) redelivers every stale continuation-due record whose ``run_tool``
    never returned. A per-pass error is logged loudly and the loop survives to the next
    interval — a silently dead reaper would strand every async park past its expiry AND
    every crash-orphaned resume, the exact failures this loop removes — while a
    cancellation (shutdown) propagates for a clean exit."""
    while True:
        await asyncio.sleep(interactions_settings().expiry_reaper_interval_seconds)
        try:
            fired = await reap_expired_parks_once()
            if fired:
                logger.info("interactions expiry reaper fired %d continuation(s)", fired)
            redelivered = await redeliver_due_continuations_once()
            if redelivered:
                logger.info("interactions reaper redelivered %d durable continuation(s)", redelivered)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("interactions expiry reaper pass failed; retrying next interval", exc_info=True)
