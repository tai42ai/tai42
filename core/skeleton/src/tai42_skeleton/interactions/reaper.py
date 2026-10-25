"""The async-park expiry reaper.

An async ``ask`` returns immediately with no blocking waiter, so nothing else
fires its expiry. This periodic loop scans the per-interaction expiry index and,
for each async park whose ``expiry_at`` has passed and that is still unanswered,
claims it ONCE — through the same atomic ``record_answer`` a human answer takes,
so an answer racing an expiry (or two reaper workers racing) resolves to exactly
one — and fires the stored continuation with the generic EXPIRY answer. The claim
drops the park from the expiry index. A due member with no pending park behind it
lapsed unresolved: it is logged at error, stated once as
``interactions_ask_expiry_abandoned``, and reconciled off the index.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from tai42_contract.interactions import PARK_COMPLETION_FAILED, InteractionRequest, InteractionResponse
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.continuation_dispatch import (
    EXPIRY_ANSWER,
    EXPIRY_ANSWERED_BY,
    continuation_due_timing,
    dispatch_continuation,
    redeliver_continuation,
)
from tai42_skeleton.interactions.giveup_delivery import deliver_park_giveup
from tai42_skeleton.interactions.kill import kill_park, redeliver_kill
from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import (
    CONTINUATION_DROPPED,
    KILL_ACT_ON_PENDING,
    KILL_DROPPED,
    InteractionStore,
)

logger = logging.getLogger(__name__)

# The platform-event topic emitted when a parked ask expires UNANSWERED — the reaper
# has just claimed it by the EXPIRY sentinel. Core states the fact; a deployment wires a
# hook on this topic in config to decide what an operator sees. It RIDES ALONGSIDE the
# expiry continuation the reaper fires — it never perturbs the claim/continuation flow.
ASK_EXPIRED_UNANSWERED_EVENT_TOPIC = "interactions_ask_expired_unanswered"

# The reason tag on the removed event + kill-due record for an expiry kill (``on_expiry="kill"``).
_EXPIRED_REASON = "expired"

# The platform-event topic emitted ONCE when a whole-run kill is PERMANENTLY abandoned — the
# kill-due reaper leg dropped its record past the give-up deadline with the driver teardown still
# failing, so no door terminal is ever committed. Best-effort, like the expiry event.
KILL_ABANDONED_EVENT_TOPIC = "interactions_kill_abandoned"

# The platform-event topic emitted ONCE when a waiting outcome is dropped UNTAKEN — the retention
# sweep found it older than the retention horizon with no subject owner having taken the resumed
# run's result. Best-effort, like the other reaper events; core states the fact, a deployment wires a
# hook to decide what an operator sees.
OUTCOME_DROPPED_UNTAKEN_EVENT_TOPIC = "interactions_outcome_dropped_untaken"

# The platform-event topic emitted ONCE when an async park's expiry is PERMANENTLY abandoned — its
# keys aged out before the expiry reaper reached it, so no ``on_expiry`` action ran and no door
# terminal was committed. Best-effort, like the other reaper events; core states the fact, a
# deployment wires a hook to decide what an operator sees.
ASK_EXPIRY_ABANDONED_EVENT_TOPIC = "interactions_ask_expiry_abandoned"


async def _emit_ask_expired_unanswered(request: InteractionRequest, group_id: str, expired_at: datetime) -> None:
    """Emit the ``interactions_ask_expired_unanswered`` platform event ONCE for a just-claimed expired park.

    Core states the fact; a deployment wires a hook on this topic to decide what an
    operator sees. Best-effort: a hooks-manager failure is logged and swallowed so it
    never perturbs the reaper's claim/continuation flow or aborts the pass — the expiry
    continuation has already been dispatched when this runs.
    """
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


async def _emit_ask_expiry_abandoned(interaction_id: str, expiry_at: datetime) -> None:
    """Emit ``interactions_ask_expiry_abandoned`` ONCE for an async park whose expiry was abandoned.

    Best-effort: a hooks-manager failure is logged and swallowed so it never perturbs the reaper
    pass. ``expiry_at`` is the park's deadline, read off its expiry-index member.
    """
    from tai42_skeleton.hooks.cache import get_hooks_manager

    payload = {"interaction_id": interaction_id, "expiry_at": expiry_at.isoformat()}
    try:
        await get_hooks_manager().on_event(topic=ASK_EXPIRY_ABANDONED_EVENT_TOPIC, payload=payload)
    except Exception:
        logger.warning(
            "interactions expiry reaper: failed to emit %r for abandoned park %s",
            ASK_EXPIRY_ABANDONED_EVENT_TOPIC,
            interaction_id,
            exc_info=True,
        )


async def _settle_unresolved_expiry(r: Redis, store: InteractionStore, interaction_id: str) -> None:
    """Close a due park this pass did not resolve: surface it once when it lapsed unresolved.

    The one exit of every expiry the reaper did not resolve. Removing the expiry-index member is
    the witness: every resolution (an answer claim, a kill prune, a cancel prune) removes the member
    in the same MULTI that ends the state, so a member this call still removes lapsed unresolved —
    it is logged at error and ``interactions_ask_expiry_abandoned`` is emitted, once, since the
    member is gone after it. A member a resolution already removed (one that raced the scan) leaves
    nothing to remove, and this returns quietly.
    """
    expiry_ms = await store.drop_expiry_member(r, interaction_id)
    if expiry_ms is None:
        return
    logger.error(
        "expiry of async park %s permanently abandoned: its keys aged out before the reaper reached it; "
        "no on_expiry action ran and no door terminal was committed",
        interaction_id,
    )
    await _emit_ask_expiry_abandoned(interaction_id, datetime.fromtimestamp(expiry_ms / 1000, UTC))


async def reap_expired_parks_once() -> int:
    """Run one reaper pass, resolving every due async park by expiry; return how many continuations fired.

    A no-op (returns 0) when the interactions store is unconfigured — the feature is OFF and
    no park can exist.
    """
    if not interactions_store_configured():
        return 0
    store = InteractionStore(interactions_settings().key_prefix)
    fired = 0
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        now = datetime.now(UTC)
        for interaction_id in await store.due_expiries(r, now):
            # Guard each member: one park that deterministically raises must not abort
            # the whole pass every interval and starve the others. Log loudly, continue.
            try:
                if await _reap_one_expired_park(r, store, interaction_id, now):
                    fired += 1
            except Exception:
                logger.error(
                    "interactions expiry reaper failed on park %s; skipping it this pass",
                    interaction_id,
                    exc_info=True,
                )
    return fired


async def _reap_one_expired_park(r: Redis, store: InteractionStore, interaction_id: str, now: datetime) -> bool:
    """Resolve a single due park by expiry; return True when this call resolved it.

    Branches on the park's stored ``on_expiry``: ``"kill"`` (the default) takes the whole run
    down through ``kill_park`` (the channel delivery withdrawn) and delivers the run's single
    FAILED — a lapsed deadline is the run failing to finish; ``"resume"`` claims the park
    and fires its stored continuation with the generic expiry answer. The
    ``interactions_ask_expired_unanswered`` event is emitted once in BOTH. A boundary member (not
    yet due) is left for a later pass. Every other exit that resolves nothing — no pending async
    park behind the member, a kill that finds the park gone, a claim that commits nothing — goes
    through :func:`_settle_unresolved_expiry`, which surfaces a park that lapsed unresolved and
    stays quiet for one a racing resolution already closed.
    """
    state = await store.get_state(r, interaction_id)
    if state is None or state.status != "pending" or state.request.mode != "async" or state.request.expiry_at is None:
        await _settle_unresolved_expiry(r, store, interaction_id)
        return False
    if now < state.request.expiry_at:
        # Indexed but not yet due (a boundary member); leave it for a later pass.
        return False
    if state.request.on_expiry == "kill":
        # Expiry kills the whole run, but claims ONLY a park still pending at claim time: the kill
        # seam's atomic pending-gate (``KILL_ACT_ON_PENDING``) is the claim, so an answer that
        # committed between this read and the claim leaves the park to its own continuation — the
        # kill writes nothing, delivers no FAILED, and returns ``"answered"`` here (not ``"pruned"``),
        # so no expiry event fires either. On a genuine expiry ``kill_park`` prunes the park, fires
        # the driver teardown, withdraws the channel delivery and delivers the run's single FAILED,
        # and the expiry event is emitted.
        result = await kill_park(
            r,
            store,
            interaction_id,
            state.group_id,
            reason=_EXPIRED_REASON,
            act_on=KILL_ACT_ON_PENDING,
            terminal=PARK_COMPLETION_FAILED,
        )
        if result == "gone":
            await _settle_unresolved_expiry(r, store, interaction_id)
            return False
        if result != "pruned":
            return False
        await _emit_ask_expired_unanswered(state.request, state.group_id, now)
        return True
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
    due_ttl, due_first_attempt_at_ms = continuation_due_timing(interactions_settings())
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
        dispatch_continuation(store, state.request, fingerprint, response)
        # State the expiry as a best-effort platform event AFTER the continuation is
        # dispatched, so a hooks failure (swallowed inside) can never come between the
        # claim and the continuation fire. Rides alongside; never perturbs the flow.
        await _emit_ask_expired_unanswered(state.request, state.group_id, now)
        return True
    await _settle_unresolved_expiry(r, store, interaction_id)
    return False


async def redeliver_due_continuations_once() -> int:
    """Run one redelivery pass, re-firing every due continuation-due record; return how many redelivered.

    These are resolves that were durably recorded but whose ``run_tool`` never returned — a
    worker crash after the claim, or an ordering race in which the resume tool raised because
    its target state was not yet persisted (so its record was never cleared). Each redelivery
    advances the record's backoff atomically before firing, so concurrent passes don't storm
    the same record; a healthy fire clears it. A no-op (returns 0) when the interactions store
    is unconfigured.
    """
    if not interactions_store_configured():
        return 0
    store = InteractionStore(interactions_settings().key_prefix)
    redelivered = 0
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        now = datetime.now(UTC)
        backoff_base_ms = int(interactions_settings().expiry_reaper_interval_seconds * 1000)
        # Cap the backoff at the record's own TTL — retrying past its lifetime is
        # pointless (the record would have TTL-expired and been reconciled off).
        backoff_cap_ms = interactions_settings().idle_ttl_seconds * 1000
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
                    # ever re-drive this resume. Surface it loudly, once, at the terminus.
                    logger.error(
                        "resume for interaction %s permanently dropped after exhausting "
                        "its retention horizon; its continuation will never re-drive",
                        interaction_id,
                    )
                    # The parking driver ends the park through the routing it captured (a chained
                    # caller, a parent run), else the PLATFORM delivers the run's single FAILED
                    # itself, off the interaction's stored delivery, keyed by the run's
                    # ``completion_id`` so it dedupes against any FAILED already delivered. The
                    # interaction's own state carries the durable ``delivery`` and the park's
                    # captured key fingerprint; when it has aged out there is no address left to
                    # deliver to, so the loud drop above stands alone.
                    state = await store.get_state(r, interaction_id)
                    if state is not None:
                        fingerprint = await store.continuation_fingerprint(r, interaction_id)
                        await deliver_park_giveup(store, state.request, fingerprint=fingerprint)
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


async def _emit_kill_abandoned(interaction_id: str, run_delivery_id: str | None) -> None:
    """Emit ``interactions_kill_abandoned`` ONCE for a permanently-abandoned whole-run kill.

    Best-effort: a hooks-manager failure is logged and swallowed so it never perturbs the reaper
    pass. ``run_delivery_id`` is the killed run's identity read off the kill-due record at give-up;
    it is ``None`` only when the record itself had already aged out (the ancient-orphan reconcile).
    """
    from tai42_skeleton.hooks.cache import get_hooks_manager

    payload = {"interaction_id": interaction_id, "run_delivery_id": run_delivery_id}
    try:
        await get_hooks_manager().on_event(topic=KILL_ABANDONED_EVENT_TOPIC, payload=payload)
    except Exception:
        logger.warning(
            "interactions kill reaper: failed to emit %r for abandoned kill %s",
            KILL_ABANDONED_EVENT_TOPIC,
            interaction_id,
            exc_info=True,
        )


async def redeliver_due_kills_once() -> int:
    """Run one kill-due redelivery pass, re-firing every due kill-due record; return how many redelivered.

    A kill-due record stands until its driver teardown returned, its channel delivery was withdrawn
    AND the run's terminal delivery committed — a worker crash between them, or a driver teardown or
    channel withdrawal that keeps raising (its ancestor not yet torn down, the channel's store
    unreachable), leaves it. Each due member is claimed with an advancing backoff, then either
    redelivered (fire the teardown + the withdrawal + the run's recorded terminal, deduped by the
    run's ``completion_id``) or,
    once past its give-up deadline with the teardown still failing, PERMANENTLY abandoned: logged at
    error, ``interactions_kill_abandoned`` emitted once carrying the killed run's
    ``{interaction_id, run_delivery_id}``, the record cleared, and NO door terminal committed (its
    clear condition never held). A no-op (returns 0) when the interactions store is unconfigured.
    """
    if not interactions_store_configured():
        return 0
    store = InteractionStore(interactions_settings().key_prefix)
    redelivered = 0
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        now = datetime.now(UTC)
        now_ms = int(now.timestamp() * 1000)
        backoff_base_ms = int(interactions_settings().expiry_reaper_interval_seconds * 1000)
        backoff_cap_ms = interactions_settings().idle_ttl_seconds * 1000
        for interaction_id in await store.due_kills(r, now):
            # Guard each member: one poison kill must not abort the whole pass. Log loudly, continue.
            try:
                due = await store.claim_kill_retry(r, interaction_id, now, backoff_base_ms, backoff_cap_ms)
                if due is None:
                    continue
                if due is KILL_DROPPED:
                    # The kill-due record TTL-expired past its backstop; only the orphan index
                    # member remained. Reconciled off by the claim — surface it, run identity gone.
                    logger.error(
                        "whole-run kill for interaction %s permanently abandoned (its kill-due record aged out); "
                        "no door terminal was committed",
                        interaction_id,
                    )
                    await _emit_kill_abandoned(interaction_id, None)
                    continue
                if now_ms >= due.deadline_ms:
                    # Past the give-up deadline with the driver teardown still failing: drop it
                    # loudly, once, committing no door terminal (the ancestor was never torn down).
                    logger.error(
                        "whole-run kill for interaction %s permanently abandoned after exhausting its "
                        "retention horizon; its teardown never completed and no door terminal was committed",
                        interaction_id,
                    )
                    await _emit_kill_abandoned(interaction_id, due.run_delivery_id)
                    await store.clear_kill_due(r, interaction_id)
                    continue
                await redeliver_kill(store, due)
                redelivered += 1
            except Exception:
                logger.error(
                    "interactions kill reaper failed redelivering kill %s; skipping it this pass",
                    interaction_id,
                    exc_info=True,
                )
    return redelivered


async def _emit_outcome_dropped_untaken(
    completion_id: str,
    *,
    interaction_id: str | None,
    run_delivery_id: str | None,
    subjects: dict[str, Any] | None,
) -> None:
    """Emit ``interactions_outcome_dropped_untaken`` ONCE for a dropped, never-taken waiting outcome.

    Best-effort: a hooks-manager failure is logged and swallowed so it never perturbs the sweep pass.
    Carries the run identity the outcome row stored. ``interaction_id``/``run_delivery_id``/
    ``subjects`` are ``None`` when the row was claimed for the drop but carried no such field, or when
    the row already left through its Redis TTL backstop and only its orphaned index member remained.
    """
    from tai42_skeleton.hooks.cache import get_hooks_manager

    payload = {
        "completion_id": completion_id,
        "interaction_id": interaction_id,
        "run_delivery_id": run_delivery_id,
        "subjects": subjects,
    }
    try:
        await get_hooks_manager().on_event(topic=OUTCOME_DROPPED_UNTAKEN_EVENT_TOPIC, payload=payload)
    except Exception:
        logger.warning(
            "interactions retention sweep: failed to emit %r for dropped outcome %s",
            OUTCOME_DROPPED_UNTAKEN_EVENT_TOPIC,
            completion_id,
            exc_info=True,
        )


async def sweep_untaken_outcomes_once() -> int:
    """Run one retention sweep dropping every waiting outcome past the retention horizon; return how many dropped.

    A resumed run's terminal parked for its subject (a waiting outcome) is taken by the subject owner
    when it next runs; one never taken must not linger past the retention horizon. Each aged outcome is
    dropped ATOMICALLY through ``claim_outcome`` — a racing subject-owner take or a concurrent sweep
    pass resolves to exactly one winner, so the drop event fires ONCE and a taken outcome never fires
    it — with ``interactions_outcome_dropped_untaken`` emitted and an error logged, so an aged outcome
    always leaves through this loud path rather than a silent Redis TTL delete (the TTL is only a
    backstop past the horizon). If a row DID already leave through that backstop, its no-TTL retention
    member is orphaned; the sweep reconciles the index and drops it just as loudly (with only the
    completion id the member carries), so the silent loss is still reported once and the orphan never
    lingers. A no-op (returns 0) when the interactions store is unconfigured.
    """
    if not interactions_store_configured():
        return 0
    store = InteractionStore(interactions_settings().key_prefix)
    dropped = 0
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        now = datetime.now(UTC)
        for completion_id in await store.due_untaken_outcomes(r, now, interactions_settings().idle_ttl_seconds):
            # Guard each member: one poison outcome must not abort the whole pass. Log loudly, continue.
            try:
                outcome = await store.claim_outcome(r, completion_id)
                if outcome is None:
                    # No row: either a subject-owner take or a racing sweep already claimed it (both
                    # drop the retention member in the same step — nothing to do), OR the row left
                    # through its Redis TTL backstop, orphaning the no-TTL retention member so this
                    # sweep keeps re-reading it. Reconcile the index; the caller that actually removed
                    # the member lost the outcome to the silent backstop — the exact loss this sweep
                    # exists to catch — so it drops loudly here with only the ids the member carries.
                    if await store.reconcile_orphan_outcome(r, completion_id):
                        logger.error(
                            "waiting outcome %s left through its Redis TTL backstop before the "
                            "retention sweep took it; no subject owner ever took the resumed run's "
                            "result and its retention-index member was orphaned",
                            completion_id,
                        )
                        await _emit_outcome_dropped_untaken(
                            completion_id, interaction_id=None, run_delivery_id=None, subjects=None
                        )
                        dropped += 1
                    continue
                logger.error(
                    "waiting outcome %s dropped untaken past its retention horizon; no subject owner "
                    "ever took the resumed run's result",
                    completion_id,
                )
                await _emit_outcome_dropped_untaken(
                    outcome.completion_id,
                    interaction_id=outcome.interaction_id,
                    run_delivery_id=outcome.run_delivery_id,
                    subjects=outcome.subjects,
                )
                dropped += 1
            except Exception:
                logger.error(
                    "interactions retention sweep failed dropping outcome %s; skipping it this pass",
                    completion_id,
                    exc_info=True,
                )
    return dropped


async def run_expiry_reaper_loop() -> None:
    """Run the expiry reaper until cancelled.

    Each pass sleeps the configured interval, then (1) resolves every due async park by
    expiry, (2) redelivers every stale continuation-due record whose ``run_tool``
    never returned, (3) redelivers every stale kill-due record whose teardown, withdrawal or terminal
    delivery never committed, and (4) drops every waiting outcome no subject owner took past the
    retention horizon. A per-pass error is logged loudly and the loop survives to the next
    interval — a silently dead reaper would strand every async park past its expiry, every
    crash-orphaned resume, every half-finished kill AND every untaken outcome, the exact failures
    this loop removes — while a cancellation (shutdown) propagates for a clean exit.
    """
    while True:
        await asyncio.sleep(interactions_settings().expiry_reaper_interval_seconds)
        try:
            fired = await reap_expired_parks_once()
            if fired:
                logger.info("interactions expiry reaper fired %d continuation(s)", fired)
            redelivered = await redeliver_due_continuations_once()
            if redelivered:
                logger.info("interactions reaper redelivered %d durable continuation(s)", redelivered)
            kills = await redeliver_due_kills_once()
            if kills:
                logger.info("interactions reaper redelivered %d durable kill(s)", kills)
            outcomes_dropped = await sweep_untaken_outcomes_once()
            if outcomes_dropped:
                logger.info("interactions reaper dropped %d untaken waiting outcome(s)", outcomes_dropped)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("interactions expiry reaper pass failed; retrying next interval", exc_info=True)
