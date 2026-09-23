"""The whole-chain kill teardown, the kill-due outbox, and the thread reverse-index reach.

Prune-or-clear a killed park atomically and enqueue the durable kill-due record, plus read and
reconcile the thread→interaction reverse index the kill cascade walks.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from redis.asyncio import Redis
from redis.exceptions import WatchError

from . import events, records, serde
from .writes_base import AskTo, _StoreWritesBase

# ``enqueue_kill``'s outcome. The three ``PruneResult`` states plus ``"skipped"``: the target was
# already resolved (answered/running) and the caller's ``act_on`` precondition excluded it, so the
# MULTI wrote NOTHING — the resolving answer's own continuation owns the run.
KillEnqueueResult = Literal["pruned", "answered", "gone", "skipped"]

# The kill's status precondition: the resolved target states a door may tear down. The whole-chain
# teardown walk (subject erase, thread/person/route delete) acts on an answered/running sibling too,
# so it never redelivers into a torn-down run; a SINGLE-park door (the expiry reaper's
# ``on_expiry="kill"`` branch, the per-interaction cancel) acts ONLY on a still-pending target, so a
# raced answer that resolved the park in the claim window is left to its own continuation.
KILL_ACT_ON_ANY: Final[frozenset[str]] = frozenset({"pending", "answered"})
KILL_ACT_ON_PENDING: Final[frozenset[str]] = frozenset({"pending"})


@dataclass(frozen=True)
class _KillPrune:
    """The denormalized state-hash fields a kill's pending-park prune MULTI writes from.

    ``remaining`` is the group's post-decrement count (``None`` when the kill carries no group, so
    the count/pending-index cleanup is skipped — a subject-erase member reached without its group).
    """

    to: AskTo
    audience: str | None
    media_ids: list[str]
    park_thread_id: str | None
    subjects: str | None
    remaining: int | None


class _StoreKillWrites(_StoreWritesBase):
    """The whole-chain kill teardown, kill-due enqueue, and thread reverse-index reach."""

    async def enqueue_kill(
        self,
        r: Redis,
        interaction_id: str,
        group_id: str | None,
        *,
        delivery: dict[str, Any] | None,
        run_delivery_id: str | None,
        subjects: dict[str, Any] | None,
        reason: str,
        kill_due_ttl: int,
        first_attempt_at_ms: int,
        deadline_ms: int,
        act_on: frozenset[str] = KILL_ACT_ON_ANY,
    ) -> KillEnqueueResult:
        """The whole-chain kill's teardown MULTI: prune the park, clear its continuation-due, enqueue the kill-due.

        Status-gated exactly like :meth:`prune_pending`, atomically under WATCH — and, when the
        target's resolved state is one the caller's ``act_on`` precondition permits, in the SAME MULTI:

        * a PENDING park is pruned (state, open/expiry/thread/subject/media indexes, count, removed
          event), returning ``"pruned"``; an ``answered`` park is not re-pruned (``"answered"``);
          a running entry whose state hash aged out while its continuation-due record still stands is
          cleared (the due record deleted, the kill-due written) and returns ``"pruned"`` too, so the
          caller tears it down now rather than deferring to the reaper; a missing state with NO
          continuation-due record is ``"gone"`` (nothing written);
        * the interaction's continuation-due record + its due-index member are deleted
          UNCONDITIONALLY of the ``pending`` status, so a buffered/answered sibling reached by the
          whole-chain walk has its live due record cleared and nothing redelivers into a run whose
          driver dropped its tombstones;
        * a durable kill-due record + its next-attempt index member are written, carrying the
          killed run's copied ``delivery`` address, ``run_delivery_id`` and subject descriptor, so
          the platform's FAILED delivery survives the prune and a crash-redelivery.

        ``act_on`` is the door's precondition — the resolved target states the kill may act on. The
        default (:data:`KILL_ACT_ON_ANY`) tears down a pending OR an already-resolved
        (answered/running) target, the whole-chain-walk behavior. A single-park door passes
        :data:`KILL_ACT_ON_PENDING`: when the WATCHed status shows the park was resolved in the claim
        window (an answer committed after the door read it pending), the resolved state is excluded,
        the MULTI writes NOTHING, and ``"skipped"`` is returned — so the answer's own continuation
        owns the run and the door delivers no FAILED, tears nothing down and clears no due record.

        The caller passes the run's ``delivery``/``run_delivery_id``/``subjects`` it read off the
        surviving record (state hash or continuation-due), and the kill-due TTL, first-attempt score
        and hard ``deadline_ms`` (the reaper's give-up horizon).
        """
        state_key = self.state_key(interaction_id)
        due_key = self.continuation_due_key(interaction_id)
        kill_key = self.kill_due_key(interaction_id)
        watch_keys = [state_key, due_key] + ([self.count_key(group_id)] if group_id is not None else [])
        kill_mapping = records._kill_due_mapping(
            reason,
            deadline_ms,
            json.dumps(delivery) if delivery is not None else None,
            run_delivery_id,
            json.dumps(subjects) if subjects is not None else None,
        )

        async with r.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(*watch_keys)
                    status = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "status")))
                    due_exists = bool(await cast("Awaitable[dict[str | bytes, str | bytes]]", pipe.hgetall(due_key)))
                    if status is None and not due_exists:
                        # Nothing live to kill: no park state and no running-entry due record.
                        await pipe.reset()
                        return "gone"
                    # The target's resolved state: pending (a live park) or answered (an
                    # answered/running entry, its state retained or aged to its due record alone).
                    resolved = "pending" if status == "pending" else "answered"
                    if resolved not in act_on:
                        # The door may act only on a still-pending target and the park was resolved in
                        # the claim window: write nothing, so the answer's own continuation owns the run.
                        await pipe.reset()
                        return "skipped"
                    prune = (
                        await self._read_kill_prune_fields(pipe, state_key, group_id) if status == "pending" else None
                    )
                    pipe.multi()
                    if prune is not None:
                        self._queue_kill_prune(pipe, interaction_id, group_id, prune, reason)
                    # UNCONDITIONAL of the pending status: a buffered/answered sibling's live due
                    # record is cleared so nothing redelivers into a torn-down run.
                    pipe.delete(due_key)
                    pipe.zrem(self.continuation_due_index_key, interaction_id)
                    pipe.hset(kill_key, mapping=kill_mapping)
                    pipe.expire(kill_key, kill_due_ttl)
                    pipe.zadd(self.kill_due_index_key, {interaction_id: first_attempt_at_ms})
                    await pipe.execute()
                except WatchError:
                    continue
                else:
                    if status == "answered":
                        return "answered"
                    # A pending park was pruned, or a running entry whose state hash aged out
                    # (``status`` is None — reached only WITH a continuation-due record present, the
                    # ``status is None and not due_exists`` case returns ``"gone"`` before the MULTI)
                    # had that due record cleared and its kill-due written: either way the entry is
                    # now torn down, so the caller delivers the FAILED and clears the kill-due at once.
                    return "pruned"

    async def _read_kill_prune_fields(self, pipe: Any, state_key: str, group_id: str | None) -> _KillPrune:
        """Read the denormalized fields a pending park's kill-prune MULTI writes from (pre-MULTI phase)."""
        audience = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "audience")))
        media_ids_field = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "media_ids")))
        media_ids = media_ids_field.split(",") if media_ids_field else []
        park_thread_id = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "thread_id")))
        to = self._addressed_to(serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "to"))))
        subjects = serde.as_str(await cast("Awaitable[str | None]", pipe.hget(state_key, "subjects")))
        remaining: int | None = None
        if group_id is not None:
            current = await pipe.get(self.count_key(group_id))
            if current is None:
                raise RuntimeError(f"pending count missing for group {group_id!r} with a live state at {state_key!r}")
            remaining = int(current) - 1
        return _KillPrune(
            to=to,
            audience=audience,
            media_ids=media_ids,
            park_thread_id=park_thread_id,
            subjects=subjects,
            remaining=remaining,
        )

    def _queue_kill_prune(
        self, pipe: Any, interaction_id: str, group_id: str | None, prune: _KillPrune, reason: str
    ) -> None:
        """Enqueue a pending park's prune writes into the kill MULTI — the same writes ``prune_pending`` makes."""
        pipe.delete(self.state_key(interaction_id))
        pipe.zrem(self._open_key_for(prune.to), interaction_id)
        pipe.zrem(self.pending_expiry_key, interaction_id)
        if prune.park_thread_id is not None:
            pipe.srem(self.thread_parks_key(prune.park_thread_id), interaction_id)
        self._queue_subject_leave(pipe, prune.subjects, interaction_id)
        if prune.media_ids and group_id is not None:
            pipe.srem(self.media_index_key(group_id), *prune.media_ids)
        if group_id is not None:
            pipe.decr(self.count_key(group_id))
            if prune.remaining is not None and prune.remaining <= 0:
                pipe.zrem(self.pending_key, group_id)
                pipe.zrem(self.pending_deadline_key, group_id)
                pipe.delete(self.count_key(group_id))
        pipe.xadd(
            self.events_key,
            cast(
                "dict[Any, Any]",
                events._event_fields(
                    events.REMOVED_EVENT, interaction_id, group_id or "", prune.audience, reason=reason
                ),
            ),
            maxlen=events._EVENTS_MAXLEN,
            approximate=True,
        )

    async def clear_kill_due(self, r: Redis, interaction_id: str) -> None:
        """Delete the kill-due record + drop its index member once the teardown and FAILED delivery committed.

        Atomic, and a no-op on an already-cleared record (a redelivery the original kill raced to
        completion), so double-clear is harmless.
        """
        pipe = r.pipeline()
        pipe.delete(self.kill_due_key(interaction_id))
        pipe.zrem(self.kill_due_index_key, interaction_id)
        await pipe.execute()

    async def thread_park_members(self, r: Redis, thread_id: str) -> list[str]:
        """The interaction ids of the async parks bound to ``thread_id`` — the thread reverse-index snapshot.

        The whole-chain kill's thread reach reads this so it can route each member through the kill
        seam. A missing/drained set is an empty list.
        """
        key = self.thread_parks_key(thread_id)
        return [serde.as_str(member) for member in await cast("Awaitable[set[str | bytes]]", r.smembers(key))]

    async def reconcile_thread_park_members(self, r: Redis, thread_id: str, members: list[str]) -> None:
        """Drop the snapshotted ``members`` from ``thread_id``'s reverse index — the kill cascade's cleanup.

        SREM only the members the cascade snapshotted (NOT a blind DELETE): a park added to the
        thread CONCURRENTLY with the cascade keeps its own member and stays cascade-cancellable on a
        retry. The kill seam's prune already dropped a pruned park's member (repeating is a no-op);
        this additionally reconciles an orphan member whose state had already vanished. A no-op on an
        empty snapshot.
        """
        if members:
            await cast("Awaitable[int]", r.srem(self.thread_parks_key(thread_id), *members))
