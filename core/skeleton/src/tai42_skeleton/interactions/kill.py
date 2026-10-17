"""The whole-run kill: the one teardown seam every withdrawal door routes through.

A kill ends a run — the tree that shares one ``run_delivery_id`` and delivers one terminal.
``kill_park`` takes the run of the park a door names down for good: nothing of the run is resumed,
nothing of it parks again, and the run's single terminal is the one the door named —
``withdrawn`` for a deliberate takedown (a cancel, an erase of the run's thread, route or person;
nothing is said to the person), ``failed`` for a lapse. It is the ONE seam the teardown doors
share — the cancel doors, the expiry reaper's ``on_expiry="kill"`` branch, and the conversation
thread/person/route deletes (through :func:`kill_members`) — so the behavior is built once and
every door reaches it.

Per kill, in order:

* the revoke of the named park (``enqueue_kill``): a status-gated prune of the park, an
  UNCONDITIONAL clear of its continuation-due record, its leave of the run index, the run's
  taken-down mark, and a durable kill-due record carrying the run's copied delivery identity, the
  ask's channel and address, its continuation tool and the door's terminal; then its channel
  delivery withdrawn (``withdraw_channel_delivery``), so its address is free when the kill
  returns. A raise here propagates to the door;
* the revoke of every OTHER park of the run, read off the run index (``run_members``), the same
  way — so every park of the run is revoked before any driver is told;
* the completion of each revoked park's kill-due record: every node of it — the park id and each
  chained-call key of its lineage — fired once through the kit kill seam (``fire_park_killed``),
  where each driver ends the super-step that node belongs to and resumes and notifies nobody;
  then the run's single terminal delivered through the platform ladder (``_deliver_terminal``),
  keyed by the run's ``completion_id`` so it lands once; then the record cleared. A completion
  that cannot finish (a driver not ready, a node nobody owns yet, any fault) is logged and keeps
  its record for the reaper's redelivery; the door is not failed.

The run's taken-down mark makes the facet refuse every resume face of the run and the park seam
refuse every new park of it, so a drive already in flight when the kill lands ends without
delivering or asking again.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from tai42_contract.interactions import PARK_COMPLETION_FAILED
from tai42_contract.states import SubjectCandidates
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.interactions.park_kill import ParkKillNotReadyError, fire_park_killed

from tai42_skeleton.channels.withdraw import withdraw_channel_delivery
from tai42_skeleton.interactions.continuation import _deliver_terminal
from tai42_skeleton.interactions.settings import interactions_settings, interactions_store_configured
from tai42_skeleton.interactions.store import KILL_ACT_ON_ANY, InteractionStore, KillDue, PruneResult

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

# The terminal outcome the platform delivers for a killed run. A delivery tool ignores this
# ``result`` (it surfaces a ``failed`` status as its uniform notice and delivers nothing for
# ``withdrawn``); a subject-tracked ``failed`` kill stores it as the failure a subject owner reads.
# Carries the removal reason for observability.
_KILL_OUTCOME_KEY: Final[str] = "tai42:run_killed"


def _candidates(subjects: dict[str, Any] | None) -> SubjectCandidates | None:
    """Rebuild the ``SubjectCandidates`` a killed run's terminal subject-tracks under; ``None`` when receiver-less."""
    if subjects is None:
        return None
    return SubjectCandidates(
        target_kind=subjects["target_kind"], target_name=subjects["target_name"], by_kind=subjects["by_kind"]
    )


async def _revoke(
    r: Redis,
    store: InteractionStore,
    interaction_id: str,
    group_id: str | None,
    *,
    reason: str,
    terminal: str,
    act_on: frozenset[str],
) -> tuple[PruneResult, KillDue | None]:
    """Revoke one park at the platform; return the store outcome and the kill-due record written, if any.

    A waiting outcome (a completed run's result) is dropped — no run to end, no channel
    reservation, no record. A run-less park (a sync question, or one that carried no run-delivery
    context) is pruned and its channel delivery withdrawn — no run to end, no record. Any other
    kind goes through ``enqueue_kill`` (prune, ticket clear, run-index leave, the run's
    taken-down mark, the kill-due record) and then its channel delivery is withdrawn; the returned
    :class:`KillDue` is the record that MULTI wrote. ``"gone"`` and a door's ``"skipped"`` write
    nothing and return no record (``"skipped"`` is reported as the ``"answered"`` state the park
    now holds). A raise propagates.
    """
    target = await store.read_kill_target(r, interaction_id)
    if target is None:
        return "gone", None
    if target.kind == "outcome":
        await store.delete_outcome(r, interaction_id)
        return "pruned", None
    if target.run_delivery_id is None:
        # No outbox for a run-less park: a raise propagates, and a crash between the prune and the
        # withdrawal leaves the channel's hold to its own deadline and to the inbound ladder's
        # no-longer-pending path.
        result = await store.prune_pending(r, interaction_id, group_id or target.group_id or "", reason=reason)
        if result == "pruned":
            await withdraw_channel_delivery(
                channel=target.channel, recipient=target.recipient, interaction_id=interaction_id
            )
        return result, None

    horizon = interactions_settings().idle_ttl_seconds
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    deadline_ms = now_ms + horizon * 1000
    result = await store.enqueue_kill(
        r,
        interaction_id,
        group_id if group_id is not None else target.group_id,
        delivery=target.delivery,
        run_delivery_id=target.run_delivery_id,
        subjects=target.subjects,
        chain_keys=target.chain_keys,
        reason=reason,
        terminal=terminal,
        channel=target.channel,
        recipient=target.recipient,
        continuation_tool=target.continuation_tool,
        # A generous TTL backstop; the record's own ``deadline_ms`` (one horizon out) governs the
        # reaper's give-up while the record still stands, so ``run_delivery_id`` is readable then.
        kill_due_ttl=2 * horizon,
        first_attempt_at_ms=now_ms + int(interactions_settings().expiry_reaper_interval_seconds * 1000),
        deadline_ms=deadline_ms,
        act_on=act_on,
    )
    if result == "gone":
        return "gone", None
    if result == "skipped":
        # A single-park door on a park answered in the claim window: nothing was written, the run
        # keeps resuming under its own continuation.
        return "answered", None
    await withdraw_channel_delivery(channel=target.channel, recipient=target.recipient, interaction_id=interaction_id)
    due = KillDue(
        interaction_id=interaction_id,
        reason=reason,
        attempts=0,
        deadline_ms=deadline_ms,
        delivery=target.delivery,
        run_delivery_id=target.run_delivery_id,
        subjects=target.subjects,
        chain_keys=target.chain_keys,
        terminal=terminal,
        channel=target.channel,
        recipient=target.recipient,
        continuation_tool=target.continuation_tool,
    )
    return result, due


async def _complete_kill(r: Redis, store: InteractionStore, due: KillDue, fired: set[str]) -> None:
    """Fire every node of ``due`` not yet in ``fired``, deliver the run's terminal, clear the record.

    The nodes are the park id (fired with its ``continuation_tool``, so the driver that binds it
    must own it) and each chained-call key of its lineage, nearest ancestor first (fired with no
    tool: some parking driver must own it). A node joins ``fired`` only once every handler
    returned, so a pass never fires a node twice and a raise leaves it to the redelivery. The
    terminal is keyed by the run's ``completion_id``, so it lands once across every record of the
    run. A raise propagates and the record stands.
    """
    for node in (due.interaction_id, *reversed(due.chain_keys)):
        if node in fired:
            continue
        await fire_park_killed(
            node,
            due.reason,
            continuation_tool=due.continuation_tool if node == due.interaction_id else None,
        )
        fired.add(node)
    await _deliver_terminal(
        store,
        interaction_id=due.interaction_id,
        outcome={_KILL_OUTCOME_KEY: due.reason},
        status=due.terminal,
        delivery=due.delivery,
        run_delivery_id=due.run_delivery_id,
        candidates=_candidates(due.subjects),
    )
    await store.clear_kill_due(r, due.interaction_id)


async def _finish_kill(r: Redis, store: InteractionStore, due: KillDue, walked: set[str]) -> None:
    """Revoke every other park of ``due``'s run, then complete every revoked park's kill-due record.

    Every park of the run not in ``walked`` (read off the run index) is revoked first, so no
    driver is told before the platform holds nothing of the run open. Each record is then
    completed (:func:`_complete_kill`) with one shared ``fired`` set, so a node common to several
    parks is fired once per pass. Nothing here fails the caller: a completion that is not ready or
    raises is logged and keeps its own record, and a raise while revoking keeps ``due``'s record —
    the reaper redelivers each and the redelivery re-reads the run index.
    """
    try:
        dues = [due]
        if due.run_delivery_id is not None:
            for member in await store.run_members(r, due.run_delivery_id):
                if member in walked:
                    continue
                walked.add(member)
                _, member_due = await _revoke(
                    r, store, member, None, reason=due.reason, terminal=due.terminal, act_on=KILL_ACT_ON_ANY
                )
                if member_due is not None:
                    dues.append(member_due)
        fired: set[str] = set()
        for each in dues:
            try:
                await _complete_kill(r, store, each, fired)
            except ParkKillNotReadyError as exc:
                logger.warning(
                    "whole-run kill of %s: %s is not ready (%s); its kill-due record stands and the reaper "
                    "redelivers it",
                    due.interaction_id,
                    each.interaction_id,
                    exc,
                )
            except Exception:
                logger.exception(
                    "whole-run kill of %s: completing %s failed; its kill-due record stands and the reaper "
                    "redelivers it",
                    due.interaction_id,
                    each.interaction_id,
                )
    except Exception:
        logger.exception(
            "whole-run kill of %s: revoking the run's parks failed; its kill-due record stands and the reaper "
            "redelivers it",
            due.interaction_id,
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
    walked: set[str] | None = None,
) -> PruneResult:
    """Take ``interaction_id``'s run down and deliver its single ``terminal``; return the named park's outcome.

    ``"pruned"`` when a pending park was torn down (or a waiting outcome / running entry cleared),
    ``"answered"`` when only an already-answered park's continuation-due was cleared, ``"gone"``
    when nothing live named the id. ``group_id`` may be ``None`` — the pending park's own group is
    read off its surviving state.

    ``act_on`` is the door's status precondition, forwarded to :meth:`InteractionStore.enqueue_kill`.
    The default (:data:`KILL_ACT_ON_ANY`) tears down a pending OR an already-resolved park. A
    single-park door passes :data:`KILL_ACT_ON_PENDING`: when the park was answered in the claim
    window, nothing is written, the answer's own continuation owns the run, and this reports
    ``"answered"``.

    When this returns, the named park is revoked and its channel delivery withdrawn (a raise there
    propagates). Every other park of its run (``walked`` — the ids this walk already reached,
    shared across it — excepted) is revoked with :data:`KILL_ACT_ON_ANY`, and every revoked park's
    nodes are fired and its terminal delivered; what cannot finish now keeps its kill-due record
    for the reaper and never fails the door.

    ``terminal`` is the door's: :data:`~tai42_contract.interactions.PARK_COMPLETION_WITHDRAWN` for
    a deliberate takedown (nothing is said to the person), the default
    :data:`~tai42_contract.interactions.PARK_COMPLETION_FAILED` for a lapse.
    """
    walked = set() if walked is None else walked
    walked.add(interaction_id)
    result, due = await _revoke(r, store, interaction_id, group_id, reason=reason, terminal=terminal, act_on=act_on)
    if due is not None:
        await _finish_kill(r, store, due, walked)
    return result


async def redeliver_kill(store: InteractionStore, due: KillDue) -> None:
    """The reaper's kill-due redelivery: re-withdraw the channel delivery, then finish the kill again.

    Reads the run's delivery identity, the killed park's lineage and continuation tool, the ask's
    channel and address and the door's terminal off the durable kill-due record (the kill already
    pruned the state), so it never re-reads a state hash. The withdrawal is idempotent; a raise
    there propagates (the reaper's per-member guard logs and leaves the record for the next
    backoff window). The run index is re-read, so a park a crashed pass never revoked is revoked
    now; every record still standing is completed by its own redelivery.
    """
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        await withdraw_channel_delivery(channel=due.channel, recipient=due.recipient, interaction_id=due.interaction_id)
        await _finish_kill(r, store, due, walked={due.interaction_id})


async def kill_members(
    r: Redis,
    store: InteractionStore,
    members: list[str],
    *,
    reason: str,
    terminal: str = PARK_COMPLETION_FAILED,
    walked: set[str] | None = None,
) -> list[str]:
    """Route each of ``members`` through :func:`kill_park` on the shared connection ``r``, de-duplicated.

    The per-member fan-out a subject/thread teardown walk runs, each member delivered the door's
    ``terminal``. ``walked`` is the set of ids the walk already reached (a fresh one when none is
    given), shared with every ``kill_park`` it routes to, so no id is torn down twice — a member of
    a run an earlier member already took down is skipped. Returns the members this call routed
    that named something live (a ``"gone"`` member — already torn down or an orphan index entry —
    and a member an earlier kill of the same walk already tore down are not in the result).
    """
    walked = set() if walked is None else walked
    reached: list[str] = []
    for member in members:
        if member in walked:
            continue
        if await kill_park(r, store, member, None, reason=reason, terminal=terminal, walked=walked) != "gone":
            reached.append(member)
    return reached


async def kill_parks_for_subject(
    kind: str, key: str, *, reason: str, terminal: str = PARK_COMPLETION_FAILED
) -> list[str]:
    """Kill every park / running entry / waiting outcome addressed to ``(kind, key)`` across every scope.

    The subject-erase entry: it walks the subject index for ``(kind, key)`` — reaching parks on
    any scope the subject ever wrote under, not only a single thread index — and routes each member
    through :func:`kill_park` with the door's ``terminal``. A no-op when the interactions store is
    unconfigured. Idempotent, so a delete's own retry re-runs it cleanly. Returns the ids reached.
    """
    if not interactions_store_configured():
        return []
    store = InteractionStore(interactions_settings().key_prefix)
    async with client_ctx(RedisClient, interactions_settings().redis) as r:
        return await kill_members(r, store, await store.subject_members(r, kind, key), reason=reason, terminal=terminal)
