"""The read model over the thread indexes (keyspaces 7-8): thread listings, transcript pages, text searches.

Each read is a page whose cost is the page, never the whole history, and every over-budget scan is
surfaced LOUDLY as truncated.
"""

from __future__ import annotations

import logging
from typing import Literal

from redis.asyncio import Redis as AsyncRedis
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.record_keys import _member, _record_matches, _thread_address_suffix
from tai42_skeleton.conversations.record_pages import ThreadPage, ThreadSummary, TranscriptPage
from tai42_skeleton.conversations.record_store_base import RecordStoreBase
from tai42_skeleton.utils.redis_typing import awaited

logger = logging.getLogger(__name__)


class RecordQueryMixin(RecordStoreBase):
    """Thread listings, transcript pages and bounded text searches (keyspaces 7-8)."""

    async def thread_exists(self, route_name: str, thread_id: str) -> bool:
        """Whether ``thread_id`` is a live thread of ``route_name`` — one ZSCORE on the route's thread index.

        The membership check a door takes before entering a thread it did not itself just create.
        """
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            score = await awaited(r.zscore(self.settings.route_threads_key(route_name), thread_id))
        return score is not None

    async def accepted_after(
        self,
        route_name: str,
        thread_id: str,
        created_at: float,
        *,
        kind: Literal["message", "event"] = "message",
        origin: Literal["client", "operator"] = "client",
    ) -> list[ConversationRecord]:
        """The thread's still-``accepted`` records accepted strictly after ``created_at``, in acceptance order.

        Reads the thread's transcript index (keyspace 7, scored by ``created_at``) for members with a
        greater score, loads each row, and keeps the ones still at ``accepted`` whose ``inbound_kind``
        and ``origin`` match — the participant ``message`` records the overlap decision gathers and the
        pending seam projects, bounded by the thread's FIFO depth (``thread_queue_depth``). An event turn
        is excluded by ``kind``; a record that has already left ``accepted`` (a follower an earlier
        decision merged or superseded, or one whose turn completed) is not returned. A member whose row
        is gone or unparseable is logged and skipped by the shared loader.
        """
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        records: list[ConversationRecord] = []
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            members = await awaited(
                r.zrangebyscore(thread_key, f"({created_at}", "+inf", start=0, num=self.settings.thread_queue_depth)
            )
            for member in members:
                record = await self._load_searched_record(r, _member(member))
                if (
                    record is not None
                    and record.delivery_status is DeliveryStatus.ACCEPTED
                    and record.inbound_kind == kind
                    and record.origin == origin
                ):
                    records.append(record)
        return records

    async def latest_thread_record(self, route_name: str, thread_id: str) -> ConversationRecord | None:
        """The newest readable record of a thread, read through its transcript index (newest first, keyspace 7).

        ``None`` when the thread has no readable record — a member whose row is gone or unparseable is
        logged and skipped by the shared loader, never a silent empty result.
        """
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            for member in await awaited(r.zrevrange(thread_key, 0, 0)):
                record = await self._load_searched_record(r, _member(member))
                if record is not None:
                    return record
        return None

    async def list_route_threads(
        self,
        route_name: str,
        *,
        offset: int,
        limit: int,
        status: frozenset[DeliveryStatus] | None = None,
        address: str | None = None,
    ) -> ThreadPage:
        """One page of ``route_name``'s threads, newest activity first.

        Each thread is summarized from its NEWEST readable record — so the cost is the page, never the
        route's whole history. Reads nothing back into the index: a thread with no readable record left is
        logged and omitted from the page, and the prune pass reclaims it.

        With a ``status`` (the summary ``last_delivery_status`` must be one of the set) or an
        ``address`` (a substring of the thread id's client-address suffix) filter, the read is
        a BOUNDED app-side post-scan — there is no per-route+status index — so ``total`` is
        the number of matches the scan found and it may report ``truncated`` (see
        :meth:`_filter_route_threads`).
        """
        key = self.settings.route_threads_key(route_name)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            if status is None and address is None:
                # Read the total the page is cut from BEFORE the page, so ``next_page`` can
                # never be computed against a count the walk itself moved.
                total = int(await awaited(r.zcard(key)))
                threads: list[ThreadSummary] = []
                for member, score in await awaited(r.zrevrange(key, offset, offset + limit - 1, withscores=True)):
                    summary = await self._thread_summary(r, route_name, _member(member), last_activity_at=float(score))
                    if summary is not None:
                        threads.append(summary)
                return ThreadPage(threads=threads, total=total)
            return await self._filter_route_threads(
                r, route_name, key, offset=offset, limit=limit, status=status, address=address
            )

    async def _filter_route_threads(
        self,
        r: AsyncRedis,
        route_name: str,
        key: str,
        *,
        offset: int,
        limit: int,
        status: frozenset[DeliveryStatus] | None,
        address: str | None,
    ) -> ThreadPage:
        """A FILTERED page of a route's threads: a bounded forward scan of the route index, newest activity first.

        Post-filters each candidate by the client address in its id (a cheap id test, applied first) and/or
        its summary ``last_delivery_status``. The per-status indexes are GLOBAL and hold ``message_id``s,
        not thread ids, so there is no direct per-route+status serve; this is the correct app-side
        post-filter.

        The scan spends at most :data:`_FILTER_THREAD_SCAN` candidates, read in windows of
        :data:`_FILTER_SCAN_WINDOW`. If it spends that budget before the index is exhausted it
        reports ``truncated`` — the page (and ``total``) is then a bounded view and matches may
        lie beyond it. ``total`` is the number of matches found; the page is the
        ``offset``/``limit`` slice of them.
        """
        matched: list[ThreadSummary] = []
        examined = 0
        rank = 0
        truncated = False
        while True:
            window = await awaited(r.zrevrange(key, rank, rank + _records._FILTER_SCAN_WINDOW - 1, withscores=True))
            if not window:
                break
            for member, score in window:
                if examined >= _records._FILTER_THREAD_SCAN:
                    truncated = True
                    break
                examined += 1
                thread_id = _member(member)
                if address is not None:
                    suffix = _thread_address_suffix(thread_id, route_name)
                    if suffix is None or address not in suffix:
                        continue
                summary = await self._thread_summary(r, route_name, thread_id, last_activity_at=float(score))
                if summary is None:
                    continue
                if status is not None and summary.last_delivery_status not in status:
                    continue
                matched.append(summary)
            if truncated:
                break
            rank += len(window)
        return ThreadPage(threads=matched[offset : offset + limit], total=len(matched), truncated=truncated)

    async def _load_searched_record(self, r: AsyncRedis, message_id: str) -> ConversationRecord | None:
        """The record ``message_id`` names, or ``None`` when its row is gone or unparseable.

        A gone or unparseable row is logged LOUDLY and left for the prune pass, never a silent skip. The
        shared read a text search's bounded scan takes for each candidate id.
        """
        hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
        if not hashed:
            logger.warning(
                "conversations: record %r is indexed but has no row; skipped in the search, left for the prune pass",
                message_id,
            )
            return None
        try:
            return self._from_hash(hashed)
        except (ValueError, KeyError):
            logger.warning("conversations: record %r is corrupt and was skipped in the search", message_id)
            return None

    async def list_thread_records(
        self,
        route_name: str,
        thread_id: str,
        *,
        offset: int,
        limit: int,
        newest_first: bool = False,
        q: str | None = None,
    ) -> TranscriptPage:
        """One page of a thread's records — oldest first by default, newest first with ``newest_first``.

        ``newest_first`` is the live-tail order, where page 1 always holds the latest messages. Either way
        the window is ``offset``/``limit`` ranks from that end of the index, which is scored by ``created_at``.

        ``total`` is 0 exactly when the index holds no record for that thread, which is how
        an unknown or fully expired thread is told from an empty page. A member whose row is
        gone or unparseable is logged and skipped; the index is left alone, so the page's
        offsets and ``total`` stay the ones the caller asked against.

        With ``q`` the read is a BOUNDED text search over the record content: each candidate
        costs a full row read (the searched text lives inside the JSON blob), so the scan
        spends at most :data:`_FILTER_RECORD_SCAN` candidates and reports ``truncated`` if it
        runs out of budget before the index is exhausted. ``total`` is then the number of
        matches found and the page is the ``offset``/``limit`` slice of them; a thread with no
        match reads as an empty page, told from an unknown thread by the empty-index short
        circuit above.
        """
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            total = int(await awaited(r.zcard(thread_key)))
            if total == 0:
                return TranscriptPage(records=[], total=0)
            if q is not None:
                matched, truncated = await self._search_thread(
                    r, thread_key, newest_first=newest_first, needle=q.lower()
                )
                return TranscriptPage(records=matched[offset : offset + limit], total=len(matched), truncated=truncated)
            records: list[ConversationRecord] = []
            end = offset + limit - 1
            window = r.zrevrange(thread_key, offset, end) if newest_first else r.zrange(thread_key, offset, end)
            for member in await awaited(window):
                record = await self._load_searched_record(r, _member(member))
                if record is not None:
                    records.append(record)
        return TranscriptPage(records=records, total=total)

    async def _search_thread(
        self, r: AsyncRedis, thread_key: str, *, newest_first: bool, needle: str
    ) -> tuple[list[ConversationRecord], bool]:
        """The records of one thread index matching ``needle``, walked in the requested order in bounded windows.

        Spends at most :data:`_FILTER_RECORD_SCAN` candidate reads. Returns ``(matches, truncated)`` —
        ``truncated`` True when the budget ran out before the index was exhausted.
        """
        matched: list[ConversationRecord] = []
        examined = 0
        rank = 0
        while True:
            end = rank + _records._FILTER_SCAN_WINDOW - 1
            window = r.zrevrange(thread_key, rank, end) if newest_first else r.zrange(thread_key, rank, end)
            members = await awaited(window)
            if not members:
                return matched, False
            for member in members:
                if examined >= _records._FILTER_RECORD_SCAN:
                    return matched, True
                examined += 1
                record = await self._load_searched_record(r, _member(member))
                if record is not None and _record_matches(record, needle):
                    matched.append(record)
            rank += len(members)

    async def list_person_thread_records(
        self,
        route_names: list[str],
        thread_id: str,
        *,
        offset: int,
        limit: int,
        newest_first: bool = False,
        q: str | None = None,
    ) -> TranscriptPage:
        """One page of a LINKED person's aggregated transcript — a k-way merge across N per-route indexes.

        Merges the same ``thread_id`` (``bridge:@person:{id}``) across the person's N per-route indexes, so
        one full history is served and never a partial slice.

        ``total`` is ``Σ zcard`` over the N indexes. For a page at ``offset``/``limit`` the
        first ``offset + limit`` members are fetched from EACH index in the requested order
        (``zrange`` ascending, ``zrevrange`` descending — the front of the page can draw at
        most that many from any one index), merged and sorted by ``(created_at, message_id)``
        in that same direction — the ``message_id`` tie-break mirrors redis's own equal-score
        member ordering, so cross-index ties page deterministically — then sliced
        ``[offset : offset + limit]``. NEVER a per-index offset (wrong global pages) and never
        an ascending fetch reversed (wrong descending window).

        With ``q`` the read is a BOUNDED text search: at most :data:`_FILTER_RECORD_SCAN`
        members are fetched from EACH index (an index that fills that window may hold more, so
        the search reports ``truncated``), merged in the requested order, then read and matched
        up to the same candidate budget. ``total`` is then the number of matches found and the
        page is the ``offset``/``limit`` slice of them.
        """
        thread_keys = [self.settings.thread_index_key(route_name, thread_id) for route_name in route_names]
        scored: list[tuple[float, str]] = []
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            total = 0
            for key in thread_keys:
                total += int(await awaited(r.zcard(key)))
            if total == 0:
                return TranscriptPage(records=[], total=0)
            if q is not None:
                return await self._search_person_thread(
                    r, thread_keys, thread_id, offset=offset, limit=limit, newest_first=newest_first, needle=q.lower()
                )
            end = offset + limit - 1
            for key in thread_keys:
                window = (
                    r.zrevrange(key, 0, end, withscores=True)
                    if newest_first
                    else r.zrange(key, 0, end, withscores=True)
                )
                scored.extend((float(score), _member(member)) for member, score in await awaited(window))
            scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=newest_first)
            records: list[ConversationRecord] = []
            for _score, message_id in scored[offset : offset + limit]:
                record = await self._load_searched_record(r, message_id)
                if record is not None:
                    records.append(record)
        return TranscriptPage(records=records, total=total)

    async def _search_person_thread(
        self,
        r: AsyncRedis,
        thread_keys: list[str],
        thread_id: str,
        *,
        offset: int,
        limit: int,
        newest_first: bool,
        needle: str,
    ) -> TranscriptPage:
        """The bounded text search over a person's aggregated transcript.

        At most :data:`_FILTER_RECORD_SCAN` members from each index (a filled window means the index may
        hold more), merged in the requested order, read and matched up to the same budget. ``truncated``
        if any index filled its window or the match scan spent its budget.
        """
        end = _records._FILTER_RECORD_SCAN - 1
        scored: list[tuple[float, str]] = []
        truncated = False
        for key in thread_keys:
            window = (
                r.zrevrange(key, 0, end, withscores=True) if newest_first else r.zrange(key, 0, end, withscores=True)
            )
            fetched = await awaited(window)
            if len(fetched) >= _records._FILTER_RECORD_SCAN:
                truncated = True
            scored.extend((float(score), _member(member)) for member, score in fetched)
        scored.sort(key=lambda entry: (entry[0], entry[1]), reverse=newest_first)
        matched: list[ConversationRecord] = []
        for examined, (_score, message_id) in enumerate(scored):
            if examined >= _records._FILTER_RECORD_SCAN:
                truncated = True
                break
            record = await self._load_searched_record(r, message_id)
            if record is not None and _record_matches(record, needle):
                matched.append(record)
        return TranscriptPage(records=matched[offset : offset + limit], total=len(matched), truncated=truncated)

    async def search_route_messages(self, route_name: str, *, offset: int, limit: int, q: str) -> TranscriptPage:
        """Every record on ``route_name`` whose text matches ``q``, across ALL the route's threads.

        A BOUNDED nested scan (the route's threads newest-active first, then each thread's records newest
        first), since there is no per-route record index. The scan is bounded in BOTH dimensions: it visits
        at most :data:`_FILTER_THREAD_SCAN` threads and reads at most :data:`_FILTER_RECORD_SCAN` records,
        reporting ``truncated`` if either budget runs out before the route is exhausted. Bounding threads
        too is load-bearing: a route of many stranded members whose per-thread index is momentarily empty
        would otherwise let the thread loop run unbounded without ever spending a record. ``total`` is the
        number of matches found; the page is the ``offset``/``limit`` slice of them. A member whose row is
        gone or unparseable is logged LOUDLY and skipped.
        """
        needle = q.lower()
        route_key = self.settings.route_threads_key(route_name)
        matched: list[ConversationRecord] = []
        examined = 0
        threads_examined = 0
        truncated = False
        thread_rank = 0
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            while not truncated:
                threads = [
                    _member(m)
                    for m in await awaited(
                        r.zrevrange(route_key, thread_rank, thread_rank + _records._FILTER_SCAN_WINDOW - 1)
                    )
                ]
                if not threads:
                    break
                for thread_id in threads:
                    if threads_examined >= _records._FILTER_THREAD_SCAN:
                        truncated = True
                        break
                    threads_examined += 1
                    thread_matches, thread_examined, hit_budget = await self._scan_thread_for_matches(
                        r, route_name, thread_id, needle, _records._FILTER_RECORD_SCAN - examined
                    )
                    matched.extend(thread_matches)
                    examined += thread_examined
                    if hit_budget:
                        truncated = True
                        break
                thread_rank += len(threads)
        return TranscriptPage(records=matched[offset : offset + limit], total=len(matched), truncated=truncated)

    async def _scan_thread_for_matches(
        self, r: AsyncRedis, route_name: str, thread_id: str, needle: str, record_budget: int
    ) -> tuple[list[ConversationRecord], int, bool]:
        """Scan ONE thread's records newest first for ``needle``, reading at most ``record_budget`` records.

        Returns ``(matches, examined, hit_budget)`` — the matching records, how many records were examined,
        and whether the budget ran out before the thread was exhausted. A member whose row is gone or
        unparseable is logged LOUDLY and skipped by the shared loader.
        """
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        matches: list[ConversationRecord] = []
        examined = 0
        member_rank = 0
        while True:
            members = [
                _member(m)
                for m in await awaited(
                    r.zrevrange(thread_key, member_rank, member_rank + _records._FILTER_SCAN_WINDOW - 1)
                )
            ]
            if not members:
                return matches, examined, False
            for message_id in members:
                if examined >= record_budget:
                    return matches, examined, True
                examined += 1
                record = await self._load_searched_record(r, message_id)
                if record is not None and _record_matches(record, needle):
                    matches.append(record)
            member_rank += len(members)

    async def _thread_summary(
        self, r: AsyncRedis, route_name: str, thread_id: str, *, last_activity_at: float
    ) -> ThreadSummary | None:
        """Summarize ``thread_id`` from the newest readable record among its newest members, or ``None``.

        Scans its :data:`_THREAD_SUMMARY_SCAN` newest members and returns ``None`` when none is readable.

        ``last_activity_at`` is the route index's own score — the value the listing SORTS
        by — passed in so the row shown and the order it is shown in can never disagree.
        Reads nothing back into the index.
        """
        thread_key = self.settings.thread_index_key(route_name, thread_id)
        for member in await awaited(r.zrevrange(thread_key, 0, _records._THREAD_SUMMARY_SCAN - 1)):
            message_id = _member(member)
            hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
            if not hashed:
                continue
            try:
                record = self._from_hash(hashed)
            except (ValueError, KeyError):
                # Keep scanning: one unreadable record must not hide a live thread from the
                # operator listing forever — its OTHER records are readable, and the prune
                # never reclaims a thread whose rows exist.
                logger.warning(
                    "conversations: record %r is corrupt and was skipped in thread %r's summary scan",
                    message_id,
                    thread_id,
                )
                continue
            return ThreadSummary(
                thread_id=thread_id,
                client_address=record.client_address,
                last_activity_at=last_activity_at,
                message_count=int(await awaited(r.zcard(thread_key))),
                last_delivery_status=record.delivery_status,
            )
        logger.warning(
            "conversations: thread %r on route %r has no readable record among its %d newest members; "
            "omitted from the listing, left for the prune pass",
            thread_id,
            route_name,
            _records._THREAD_SUMMARY_SCAN,
        )
        return None
