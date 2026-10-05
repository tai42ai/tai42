"""The whole-chain kill: the one teardown seam every withdrawal door routes through.

``kill_park`` tears a parked (or running) run down for good and delivers its single terminal — the
status the DOOR chose (``failed`` for an expiry/break, ``withdrawn`` for a deliberate cancel or
erase). It is the ONE seam the teardown doors share — the cancel door, the expiry reaper's
``on_expiry="kill"`` branch, and the conversation thread/person/route deletes (through
:func:`kill_parks_for_subject`) — so the whole-chain behavior is built once and every door reaches
it.

Per kill, in order:

* the store teardown MULTI (``enqueue_kill``): a status-gated prune of the park, an UNCONDITIONAL
  clear of its continuation-due record + due-index member (so a buffered/answered sibling never
  redelivers into a torn-down run), and a durable kill-due record carrying the killed run's own
  copied delivery identity, the door's chosen terminal, and the ask's channel/recipient — so the
  terminal delivery and the channel withdrawal survive a crash and the reaper redelivers;
* the driver teardown fire (``fire_park_killed``) with the run-authorization context BOUND around
  it (``resume_origin`` = the killed interaction, the killed run's ``run_delivery_id`` re-established
  as the ambient), so a handler's cross-driver teardown notify authorizes on the shared delivery
  identity. A handler that RAISES propagates — the kill-due record is kept and the reaper redelivers;
* the channel withdraw (``withdraw_channel_delivery``): the delivering channel releases the slot it
  reserved for the ask, BEFORE any terminal, so a door that cancels-then-asks finds the slot free
  when the cancel returns;
* the run's single terminal delivered ONCE through the platform ladder (``_deliver_terminal``), keyed
  by the run's ``completion_id`` off the copied ``delivery``/``run_delivery_id`` — the door terminal
  for a receiver-started run, else a waiting outcome on its subject (only for ``failed``; a
  ``withdrawn`` run has no outcome), else a drop;
* the kill-due record cleared only after the teardown returned, the withdraw ran, and the terminal
  committed.
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

from tai42_skeleton.channels.withdraw import withdraw_channel_delivery
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
    terminal: str,
    channel: str | None,
    recipient: str | None,
) -> None:
    """Fire the driver teardown, WITHDRAW the channel reservation, then deliver the run's single terminal.

    Runs, in order: (1) ``fire_park_killed`` with the run-authorization context bound (origin = the
    killed interaction, ambient run-delivery = the killed run's identity) so a handler's cross-driver
    teardown notify authorizes on the shared ``run_delivery_id``; (2) ``withdraw_channel_delivery`` so
    the channel frees the slot the delivered ask held — BEFORE any terminal, so a door that
    cancels-then-asks finds the slot free when the cancel returns; (3) ``_deliver_terminal`` with the
    door's chosen ``terminal`` status. A raise in (1) or (2) propagates exactly like a handler raise —
    the caller keeps the kill-due record and the reaper redelivers all three idempotently (the
    handlers by contract, the withdraw by its compare-and-delete, the terminal by ``completion_id``).
    The terminal is keyed by the run's ``completion_id`` off ``run_delivery_id``, so a kill entering
    at ANY interaction of the run delivers once and any second fire dedupes.
    """
    delivery_tuple = _delivery_tuple(delivery)
    bound: RunDelivery | None = RunDelivery(run_delivery_id, delivery_tuple) if run_delivery_id is not None else None
    with resume_origin(interaction_id), run_delivery(bound):
        await fire_park_killed(interaction_id, reason)
    await withdraw_channel_delivery(channel=channel, recipient=recipient, interaction_id=interaction_id)
    await _deliver_terminal(
        store,
        interaction_id=interaction_id,
        outcome={_KILL_OUTCOME_KEY: reason},
        status=terminal,
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
    terminal: str = PARK_COMPLETION_FAILED,
) -> PruneResult:
    """Tear ``interaction_id``'s run down whole-chain and deliver its single terminal; return the store outcome.

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
    with no teardown fire and no terminal. A run-less park (a sync question, or one that carried no
    run-delivery context) is pruned, then its channel reservation withdrawn. Every other kind fires
    the driver teardown, withdraws the channel reservation, and delivers the run's single terminal
    once.

    ``terminal`` is the status the DOOR chose for the delivered terminal, never derived from the
    ``reason`` string: :data:`~tai42_contract.interactions.PARK_COMPLETION_FAILED` (the default — the
    run broke or lapsed, the person reads the uniform notice) or
    :data:`~tai42_contract.interactions.PARK_COMPLETION_WITHDRAWN` (the platform took the run down on
    purpose — a cancel or a thread/person erase — nothing is said to the person). It rides the
    kill-due record so a redelivery delivers the same one.
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
        # down whole-chain and no terminal to deliver. A plain status-gated prune, then the channel
        # reservation is withdrawn so a delivered ask frees its slot at once; a raise propagates
        # loudly (there is no outbox for a run-less park — the inbound-404 path and the TTL are the
        # backstop for a crash between the prune and the release).
        result = await store.prune_pending(r, interaction_id, group_id or target.group_id or "", reason=reason)
        if result == "pruned":
            await withdraw_channel_delivery(
                channel=target.channel, recipient=target.recipient, interaction_id=interaction_id
            )
        return result

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
        terminal=terminal,
        channel=target.channel,
        recipient=target.recipient,
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
        terminal=terminal,
        channel=target.channel,
        recipient=target.recipient,
    )
    await store.clear_kill_due(r, interaction_id)
    return result


async def redeliver_kill(store: InteractionStore, due: KillDue) -> None:
    """The reaper's kill-due redelivery: re-fire the driver teardown + the channel withdraw + the terminal.

    Reads the run's delivery identity, chosen ``terminal`` and the ask's ``channel``/``recipient`` off
    the durable kill-due record (the kill already pruned the state), so it never re-reads a state
    hash. A handler or withdraw raise propagates (the reaper's per-member guard logs and leaves the
    record for the next backoff window); a clean pass clears the record.
    """
    await _teardown_and_deliver(
        store,
        interaction_id=due.interaction_id,
        delivery=due.delivery,
        run_delivery_id=due.run_delivery_id,
        subjects=due.subjects,
        reason=due.reason,
        terminal=due.terminal,
        channel=due.channel,
        recipient=due.recipient,
    )
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        await store.clear_kill_due(r, due.interaction_id)


async def kill_members(
    r: Redis, store: InteractionStore, members: list[str], *, reason: str, terminal: str = PARK_COMPLETION_FAILED
) -> list[str]:
    """Route each of ``members`` through :func:`kill_park` on the shared connection ``r``, de-duplicated.

    The per-member fan-out a subject/thread teardown walk runs. ``terminal`` is the status every
    member's terminal is delivered with (a subject/thread/person erase passes
    :data:`~tai42_contract.interactions.PARK_COMPLETION_WITHDRAWN`). Returns the ids that named
    something live (a ``"gone"`` member — already torn down, or an orphan index entry — is skipped
    from the result).
    """
    seen: set[str] = set()
    reached: list[str] = []
    for member in members:
        if member in seen:
            continue
        seen.add(member)
        if await kill_park(r, store, member, None, reason=reason, terminal=terminal) != "gone":
            reached.append(member)
    return reached


async def kill_parks_for_subject(
    kind: str, key: str, *, reason: str, terminal: str = PARK_COMPLETION_FAILED
) -> list[str]:
    """Kill every park / running entry / waiting outcome addressed to ``(kind, key)`` across every scope.

    The subject-erase entry: it walks the subject index for ``(kind, key)`` — reaching parks on
    any scope the subject ever wrote under, not only a single thread index — and routes each member
    through :func:`kill_park` with the door's chosen ``terminal``. A no-op when the interactions store
    is unconfigured. Idempotent, so a delete's own retry re-runs it cleanly. Returns the ids reached.
    """
    if not interactions_store_configured():
        return []
    settings = interactions_settings()
    store = InteractionStore(settings.key_prefix)
    async with client_ctx(RedisClient, settings.redis) as r:
        return await kill_members(r, store, await store.subject_members(r, kind, key), reason=reason, terminal=terminal)
