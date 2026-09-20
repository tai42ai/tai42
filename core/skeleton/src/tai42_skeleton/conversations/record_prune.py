"""The retention prune: the only place either thread index shrinks outside a record delete.

Plus the route/thread teardown a route or thread delete runs and the resumable cursor a pass
returns.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass

from redis.asyncio import Redis as AsyncRedis
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.models import TERMINAL_STATUSES
from tai42_skeleton.conversations.record_keys import _member, _resume_at
from tai42_skeleton.conversations.record_scripts import _DELETE_LUA, _INDEXED_STATUSES, _PRUNE_THREAD_LUA
from tai42_skeleton.conversations.record_store_base import RecordStoreBase
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PruneCursor:
    """Where a prune pass stopped, so the next one RESUMES there.

    Resuming avoids re-reading the same head of the same index forever.

    A record carries no expiry until it turns terminal, so an index member can be older
    than the retention window while its row is still very much alive. A run of such members
    at the front of a thread — or a run of such threads at the front of a route — would
    absorb every pass's budget and the reclaimable members behind them would never be
    reached. The cursor is what turns that live-lock into forward progress.

    ``route_name`` is the route the pass stopped in, ``thread_rank`` the rank it stopped at
    in that route's thread index, and ``member_rank`` the rank it stopped at inside that
    thread's own index. A cursor naming a route that no longer exists simply starts the
    next pass from the beginning.

    ``thread_id`` pins ``member_rank`` to the thread it was measured in. A thread that
    leaves the route index between two passes shifts every thread behind it down one rank,
    so the rank alone would carry a member offset into a DIFFERENT thread and silently skip
    that thread's first members; the next pass applies ``member_rank`` only when the thread
    standing at ``thread_rank`` is still this one.
    """

    route_name: str | None = None
    thread_rank: int = 0
    member_rank: int = 0
    thread_id: str | None = None


#: The cursor a pass starts (and wraps) at: the first thread of the first route.
PRUNE_START = PruneCursor()


@dataclass(frozen=True)
class _ThreadPruneStep:
    """What draining one thread cost and where it left off.

    ``resume_rank`` is ``None`` exactly when the thread has no expired candidate left to offer;
    ``emptied`` says the thread's index ran empty, which took it out of the route index and
    shifted the ranks of every thread behind it down by one.
    """

    spent: int
    resume_rank: int | None
    emptied: bool


class RecordPruneMixin(RecordStoreBase):
    """Reclamation and teardown of the thread/route indexes (keyspaces 7-8)."""

    async def prune_expired_terminal_indexes(
        self, route_names: Iterable[str], cursor: PruneCursor = PRUNE_START
    ) -> PruneCursor:
        """Drop the index members no read reaches, so neither index outgrows the keyspace it names.

        The retained keyspace stays bounded: the ONLY place either thread index shrinks outside
        a record delete — the reads deliberately leave it alone.

        Terminal-status members are dropped by score: a member's score is its row's exact
        expiry moment, and the ``delivered``/``shed``/``silent`` indexes are read by nothing
        (the live and on-demand ones are pruned lazily on every read).

        Every thread of every named route is then offered its expired members — a member
        scored older than the retention window whose row is indeed gone — so a long-lived
        ACTIVE thread stops over-reporting its ``message_count`` too, not just an idle one.

        One pass is BOUNDED in every dimension: it reads at most
        :data:`_PRUNE_THREADS_PER_BATCH` threads and :data:`_PRUNE_MEMBERS_PER_BATCH`
        members per command, and spends at most :data:`_PRUNE_WORK_PER_PASS` units of work,
        one per thread EXAMINED plus one per candidate member offered. A route holding a
        hundred thousand healthy threads therefore costs a fixed pass, not a hundred
        thousand round trips.

        Takes the cursor the previous pass returned and returns where this one stopped, so
        the work left over is picked up from there rather than re-read from the front (see
        :class:`PruneCursor`). A pass that walked every route returns
        :data:`PRUNE_START`.
        """
        now = time.time()
        expired_before = now - self.settings.answer_retention_ttl_seconds
        budget = _records._PRUNE_WORK_PER_PASS
        routes = _resume_at(list(route_names), cursor.route_name)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            for status in TERMINAL_STATUSES:
                await awaited(r.zremrangebyscore(self.settings.status_index_key(status.value), "-inf", now))
            for position, route_name in enumerate(routes):
                resuming = position == 0 and route_name == cursor.route_name
                spent, stopped = await self._prune_route(
                    r,
                    route_name,
                    expired_before,
                    thread_rank=cursor.thread_rank if resuming else 0,
                    member_rank=cursor.member_rank if resuming else 0,
                    member_thread=cursor.thread_id if resuming else None,
                    budget=budget,
                )
                budget -= spent
                if stopped is not None:
                    return stopped
                if budget <= 0:
                    following = routes[position + 1 :]
                    return PruneCursor(following[0]) if following else PRUNE_START
        return PRUNE_START

    async def _prune_route(
        self,
        r: AsyncRedis,
        route_name: str,
        expired_before: float,
        *,
        thread_rank: int,
        member_rank: int,
        member_thread: str | None,
        budget: int,
    ) -> tuple[int, PruneCursor | None]:
        """Walk one route's threads from ``thread_rank`` on, spending at most ``budget`` units of work.

        Walks in rank windows of :data:`_PRUNE_THREADS_PER_BATCH`.

        Returns ``(spent, stopped)``, where ``stopped`` is the cursor to resume from when
        the budget ran out mid-route and ``None`` when the route was walked to its end. A
        thread that empties leaves the route index and shifts the threads behind it down one
        rank, so the walking rank advances only over the threads that SURVIVED — the next
        window can then neither skip a thread nor re-read one.

        ``member_rank`` is applied only to ``member_thread``: between two passes a thread at
        a LOWER rank can vanish and shift this one out from under the rank the last pass
        recorded, and a member offset carried into a different thread would silently skip
        that thread's first members.
        """
        key = self.settings.route_threads_key(route_name)
        spent = 0
        rank = thread_rank
        resume_member = member_rank
        resume_thread = member_thread
        while spent < budget:
            members = await awaited(r.zrange(key, rank, rank + _records._PRUNE_THREADS_PER_BATCH - 1))
            if not members:
                return spent, None
            for member in members:
                thread_id = _member(member)
                if thread_id != resume_thread:
                    # Not the thread the member offset was measured in: start it from rank 0.
                    resume_member, resume_thread = 0, thread_id
                if spent >= budget:
                    return spent, PruneCursor(route_name, rank, resume_member, resume_thread)
                step = await self._prune_thread(
                    r,
                    route_name,
                    thread_id,
                    expired_before,
                    start_rank=resume_member,
                    budget=budget - spent - 1,
                )
                # One unit for EXAMINING the thread, so a route of healthy threads — each
                # costing one ranged read and buying no member work — is bounded too.
                spent += 1 + step.spent
                resume_member, resume_thread = 0, None
                if step.resume_rank is not None:
                    return spent, PruneCursor(route_name, rank, step.resume_rank, thread_id)
                if not step.emptied:
                    rank += 1
        return spent, PruneCursor(route_name, rank, 0)

    async def _prune_thread(
        self,
        r: AsyncRedis,
        route_name: str,
        thread_id: str,
        expired_before: float,
        *,
        start_rank: int,
        budget: int,
    ) -> _ThreadPruneStep:
        """Offer one thread's retention-expired candidates to the atomic prune step, from ``start_rank`` on.

        Offered one bounded batch at a time until the thread has none left or ``budget`` units
        of work are spent.

        A candidate whose row is still there is NOT removed, so the rank of the next
        unexamined member is the rank walked to minus the members the step did remove.

        The first batch always runs, so a thread reached with no budget left still costs one
        ranged read: the pass may overshoot by one batch per thread rather than charge for
        work it did not do.

        A thread with NO candidate is still offered to the step, so one whose index has run
        empty leaves the route index here. Nothing else would ever drop it: the reads
        deliberately leave the index alone, and a thread that can offer no member would
        otherwise be examined and skipped by every pass forever, over-counting the route's
        ``total``, re-logging the read doors' orphan warning and blocking a door edit behind
        a count with no visible thread.
        """
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        route_key = self.settings.route_threads_key(route_name)
        spent = 0
        rank = start_rank
        while True:
            candidates = [
                _member(member)
                for member in await awaited(
                    r.zrangebyscore(
                        thread_key, "-inf", expired_before, start=rank, num=_records._PRUNE_MEMBERS_PER_BATCH
                    )
                )
            ]
            if not candidates:
                _, remaining = await eval_script(r, _PRUNE_THREAD_LUA, 2, thread_key, route_key, thread_id)
                return _ThreadPruneStep(spent, None, emptied=int(remaining) == 0)
            keys = [thread_key, route_key]
            keys.extend(self.settings.record_key(message_id) for message_id in candidates)
            removed, remaining = await eval_script(r, _PRUNE_THREAD_LUA, len(keys), *keys, thread_id, *candidates)
            spent += len(candidates)
            rank += len(candidates) - int(removed)
            if int(remaining) == 0:
                return _ThreadPruneStep(spent, None, emptied=True)
            if len(candidates) < _records._PRUNE_MEMBERS_PER_BATCH:
                return _ThreadPruneStep(spent, None, emptied=False)
            if spent >= budget:
                return _ThreadPruneStep(spent, rank, emptied=False)

    async def count_route_threads(self, route_name: str) -> int:
        """How many threads ``route_name``'s index holds.

        The count the delete door reads to tell an unknown route from one whose reclamation was
        interrupted and is owed.
        """
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(await awaited(r.zcard(self.settings.route_threads_key(route_name))))

    async def route_thread_ids(self, route_name: str) -> list[str]:
        """Every thread id currently in ``route_name``'s thread index.

        The route delete's work list for a per-thread cascade (parked-ask cancellation) that
        must run BEFORE :meth:`drop_route_threads` tears the indexes down. A plain read that
        reclaims nothing; the index itself is walked and dropped by ``drop_route_threads``. Read
        in full because the cascade must reach every thread the route owns, not a page of them.
        """
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            key = self.settings.route_threads_key(route_name)
            return [_member(member) for member in await awaited(r.zrange(key, 0, -1))]

    async def drop_route_threads(self, route_name: str) -> None:
        """Delete a route's thread indexes.

        Every per-thread transcript ZSET the route's thread index names and each thread's mode
        override, then the index itself. Neither the transcript indexes nor the route index
        carry a TTL and the prune pass only walks LIVE routes, so a deleted route's indexes are
        unreachable unless its delete reclaims them here. The records themselves are left to
        their own retention TTL.

        Walked in rank windows of :data:`_PRUNE_THREADS_PER_BATCH`, so no single reply
        carries a whole route's threads; each window leaves the route index as it is
        handled, which is what makes re-reading rank 0 walk forward, and what makes an
        interrupted run RESUMABLE: a thread's own index is deleted before the thread leaves
        the route index, so the route index still names every thread a stopped run had not
        finished with, and re-running this reclaims exactly the remainder. That index is
        therefore the durable marker a repeated delete finds the work by — which is why the
        delete door treats a name with a surviving route index as reclaimable rather than
        as an unknown route.
        """
        key = self.settings.route_threads_key(route_name)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            while True:
                members = [
                    _member(member) for member in await awaited(r.zrange(key, 0, _records._PRUNE_THREADS_PER_BATCH - 1))
                ]
                if not members:
                    break
                for thread_id in members:
                    await awaited(r.delete(self.settings.thread_index_key(route_name, thread_id)))
                    await awaited(r.delete(self.settings.mode_key(thread_id)))
                await awaited(r.zrem(key, *members))
            await awaited(r.delete(key))

    async def drop_thread(self, route_name: str, thread_id: str) -> int:
        """Delete one thread under ``route_name`` outright.

        Removes every answer record its transcript index names, that index, the thread's
        membership in the route index, and its mode override. Returns the number of record ROWS
        removed (an index member whose row already expired counts 0 yet is still unindexed).

        RETRYABLE: draining the transcript index reclaims the route member with the last
        record (the same atomic step a record delete takes), so an interrupted run leaves the
        still-indexed remainder for a re-run, and the trailing deletes cover an index already
        emptied and a route member stranded without one. Neither thread index carries a TTL and
        the prune pass walks LIVE routes only, so nothing here may leave a member behind: the
        operator asked for the thread gone now, not on the records' own retention clock.
        """
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        route_key = self.settings.route_threads_key(route_name)
        status_keys = [self.settings.status_index_key(status.value) for status in _INDEXED_STATUSES]
        removed = 0
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            while True:
                members = [
                    _member(member)
                    for member in await awaited(r.zrange(thread_key, 0, _records._PRUNE_MEMBERS_PER_BATCH - 1))
                ]
                if not members:
                    break
                for message_id in members:
                    keys = [self.settings.record_key(message_id), *status_keys, thread_key, route_key]
                    removed += int(await eval_script(r, _DELETE_LUA, len(keys), *keys, message_id, thread_id))
            await awaited(r.delete(thread_key))
            await awaited(r.zrem(route_key, thread_id))
            await awaited(r.delete(self.settings.mode_key(thread_id)))
        return removed
