"""The whole-chain kill: the one teardown seam every withdrawal door routes through.

``kill_park`` tears a parked (or running) run down for good and delivers its single FAILED. It is
the ONE seam the teardown doors share — the cancel door, the expiry reaper's ``on_expiry="kill"``
branch, and the conversation thread/person/route deletes (through :func:`kill_parks_for_subject`)
— so the whole-chain behavior is built once and every door reaches it.

Per kill, in order:

* the store teardown MULTI (``enqueue_kill``): a status-gated prune of the park, an UNCONDITIONAL
  clear of its continuation-due record + due-index member (so a buffered/answered sibling never
  redelivers into a torn-down run), and a durable kill-due record carrying the killed run's own
  copied delivery identity — so the FAILED delivery survives a crash and the reaper redelivers;
* the driver teardown fire (``fire_park_killed``) with the run-authorization context BOUND around
  it (``resume_origin`` = the killed interaction, the killed run's ``run_delivery_id`` re-established
  as the ambient), so a handler's cross-driver teardown notify authorizes on the shared delivery
  identity. A handler that RAISES propagates — the kill-due record is kept and the reaper redelivers;
* the run's single FAILED delivered ONCE through the platform ladder (``_deliver_terminal``), keyed
  by the run's ``completion_id`` off the copied ``delivery``/``run_delivery_id`` — the door FAILED
  for a receiver-started run, else a ``failed`` waiting outcome on its subject, else a drop;
* the kill-due record cleared only after both the teardown returned and the FAILED committed.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from tai42_contract.interactions import PARK_COMPLETION_FAILED, fire_park_killed
from tai42_contract.states import SubjectCandidates
from tai42_contract.tools import RunDelivery, run_delivery
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.interactions.continuation import _deliver_terminal
from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import KILL_ACT_ON_ANY, InteractionStore, KillDue, PruneResult
from tai42_skeleton.runs.chokepoint import resume_origin

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# The failed-terminal outcome the platform delivers for a killed run. The delivery tool surfaces a
# FAILED status as its uniform notice and ignores this ``result``; a subject-tracked kill stores it
# as the failure a subject owner reads. Carries the removal reason for observability.
_KILL_OUTCOME_KEY: Final[str] = "tai42:run_killed"


def _delivery_tuple(delivery: dict[str, Any] | None) -> tuple[str | None, Mapping[str, Any] | None] | None:
    """Shape a stored ``{tool, context}`` delivery into the ``(tool, context)`` the ladder fires, or ``None``."""
    if delivery is None:
        return None
    return delivery.get("tool"), delivery.get("context")


def _candidates(subjects: dict[str, Any] | None) -> SubjectCandidates | None:
    """Rebuild the ``SubjectCandidates`` a killed run's FAILED subject-tracks under, or ``None`` when receiver-less."""
    if subjects is None:
        return None
    return SubjectCandidates(
        target_kind=subjects["target_kind"], target_name=subjects["target_name"], by_kind=subjects["by_kind"]
    )


async def _teardown_and_deliver(
    store: InteractionStore,
    *,
    interaction_id: str,
    delivery: dict[str, Any] | None,
    run_delivery_id: str | None,
    subjects: dict[str, Any] | None,
    reason: str,
) -> None:
    """Fire the driver teardown, then deliver the killed run's single FAILED through the platform ladder.

    Binds the run-authorization context around ``fire_park_killed`` (origin = the killed
    interaction, ambient run-delivery = the killed run's identity) so a handler's cross-driver
    teardown notify authorizes on the shared ``run_delivery_id``. A handler raise propagates (the
    caller keeps the kill-due record for the reaper). The FAILED is keyed by the run's
    ``completion_id`` off ``run_delivery_id``, so a kill entering at ANY interaction of the run
    delivers once and any second fire dedupes.
    """
    delivery_tuple = _delivery_tuple(delivery)
    bound: RunDelivery | None = RunDelivery(run_delivery_id, delivery_tuple) if run_delivery_id is not None else None
    with resume_origin(interaction_id), run_delivery(bound):
        await fire_park_killed(interaction_id, reason)
    await _deliver_terminal(
        store,
        interaction_id=interaction_id,
        outcome={_KILL_OUTCOME_KEY: reason},
        status=PARK_COMPLETION_FAILED,
        delivery=delivery_tuple,
        run_delivery_id=run_delivery_id,
        candidates=_candidates(subjects),
    )


async def kill_park(
    r: Redis,
    store: InteractionStore,
    interaction_id: str,
    group_id: str | None,
    *,
    reason: str,
    act_on: frozenset[str] = KILL_ACT_ON_ANY,
) -> PruneResult:
    """Tear ``interaction_id``'s run down whole-chain and deliver its single FAILED; return the store outcome.

    ``"pruned"`` when a pending park was torn down (or a waiting outcome / running entry cleared),
    ``"answered"`` when only an already-answered sibling's continuation-due was cleared, ``"gone"``
    when nothing live named the id. ``group_id`` may be ``None`` — the pending park's own group is
    read off its surviving state.

    ``act_on`` is the door's status precondition, forwarded to :meth:`InteractionStore.enqueue_kill`.
    The default (:data:`KILL_ACT_ON_ANY`) tears down a pending OR an already-resolved sibling — the
    whole-chain-walk behavior. A single-park door passes :data:`KILL_ACT_ON_PENDING`: when the park
    was answered in the claim window, ``enqueue_kill`` writes nothing and returns ``"skipped"``, so
    this delivers NO FAILED, tears nothing down and clears no due record — the answer's own
    continuation owns the run — and reports ``"answered"``.

    A waiting outcome (a completed run's result) has no live run to tear down: it is simply dropped,
    with no teardown fire and no FAILED. A run-less park (a sync question, or one that carried no
    run-delivery context) is plainly pruned. Every other kind fires the driver teardown and delivers
    the run's single FAILED once.
    """
    target = await store.read_kill_target(r, interaction_id)
    if target is None:
        return "gone"
    if target.kind == "outcome":
        # A completed run's waiting result — nothing to resume or fail; drop it and its index.
        await store.delete_outcome(r, interaction_id)
        return "pruned"
    if target.run_delivery_id is None:
        # A sync question, or a park that carried no run-delivery context: there is no run to tear
        # down whole-chain and no address to deliver a FAILED to. A plain status-gated prune.
        return await store.prune_pending(r, interaction_id, group_id or target.group_id or "", reason=reason)

    settings = interactions_settings()
    horizon = settings.idle_ttl_seconds
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    result = await store.enqueue_kill(
        r,
        interaction_id,
        group_id if group_id is not None else target.group_id,
        delivery=target.delivery,
        run_delivery_id=target.run_delivery_id,
        subjects=target.subjects,
        reason=reason,
        # A generous TTL backstop; the record's own ``deadline_ms`` (one horizon out) governs the
        # reaper's give-up while the record still stands, so ``run_delivery_id`` is readable then.
        kill_due_ttl=2 * horizon,
        first_attempt_at_ms=now_ms + int(settings.expiry_reaper_interval_seconds * 1000),
        deadline_ms=now_ms + horizon * 1000,
        act_on=act_on,
    )
    if result == "gone":
        return "gone"
    if result == "skipped":
        # A single-park door on a park answered in the claim window: nothing was written, the run
        # keeps resuming under its own continuation. Report it as the answered state it now holds.
        return "answered"
    await _teardown_and_deliver(
        store,
        interaction_id=interaction_id,
        delivery=target.delivery,
        run_delivery_id=target.run_delivery_id,
        subjects=target.subjects,
        reason=reason,
    )
    await store.clear_kill_due(r, interaction_id)
    return result


async def redeliver_kill(store: InteractionStore, due: KillDue) -> None:
    """The reaper's kill-due redelivery: re-fire the driver teardown + the run's FAILED, then clear the record.

    Reads the run's delivery identity off the durable kill-due record (the kill already pruned the
    state), so it never re-reads a state hash. A handler raise propagates (the reaper's per-member
    guard logs and leaves the record for the next backoff window); a clean pass clears the record.
    """
    await _teardown_and_deliver(
        store,
        interaction_id=due.interaction_id,
        delivery=due.delivery,
        run_delivery_id=due.run_delivery_id,
        subjects=due.subjects,
        reason=due.reason,
    )
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        await store.clear_kill_due(r, due.interaction_id)


async def kill_members(r: Redis, store: InteractionStore, members: list[str], *, reason: str) -> list[str]:
    """Route each of ``members`` through :func:`kill_park` on the shared connection ``r``, de-duplicated.

    The per-member fan-out a subject/thread teardown walk runs. Returns the ids that named something
    live (a ``"gone"`` member — already torn down, or an orphan index entry — is skipped from the
    result).
    """
    seen: set[str] = set()
    reached: list[str] = []
    for member in members:
        if member in seen:
            continue
        seen.add(member)
        if await kill_park(r, store, member, None, reason=reason) != "gone":
            reached.append(member)
    return reached


async def kill_parks_for_subject(kind: str, key: str, *, reason: str) -> list[str]:
    """Kill every park / running entry / waiting outcome addressed to ``(kind, key)`` across every scope.

    The subject-erase entry: it walks the subject index for ``(kind, key)`` — reaching parks on
    any scope the subject ever wrote under, not only a single thread index — and routes each member
    through :func:`kill_park`. A no-op when the interactions store is unconfigured. Idempotent, so a
    delete's own retry re-runs it cleanly. Returns the ids reached.
    """
    if not interactions_store_configured():
        return []
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        return await kill_members(r, store, await store.subject_members(r, kind, key), reason=reason)
