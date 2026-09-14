"""The per-status record index (keyspace 6): the delivery sweep's work listing, the status
listing, and the in-flight-intake check, each reading the index rather than the whole
retained keyspace."""

from __future__ import annotations

import json
import logging
import time

from redis.asyncio import Redis as AsyncRedis
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.models import ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.record_keys import _member
from tai42_skeleton.conversations.record_pages import PendingWork
from tai42_skeleton.conversations.record_scripts import (
    _F_ATTEMPTS,
    _F_DATA,
    _F_GRACE,
    _F_INTAKE,
    _F_STATUS,
    _UNINDEX_LUA,
)
from tai42_skeleton.conversations.record_store_base import RecordStoreBase
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)


class RecordIndexMixin(RecordStoreBase):
    """Reads over the per-status record index (keyspace 6)."""

    async def _indexed_ids(self, r: AsyncRedis, statuses: frozenset[DeliveryStatus], now: float) -> list[str]:
        """The ``message_id``s indexed under ``statuses``, dropping the members whose row
        has expired out from under the index first."""
        ids: list[str] = []
        for status in statuses:
            key = self.settings.status_index_key(status.value)
            await awaited(r.zremrangebyscore(key, "-inf", now))
            ids.extend(
                member.decode() if isinstance(member, bytes) else member
                for member in await awaited(r.zrange(key, 0, -1))
            )
        return ids

    async def _drop_orphan(self, r: AsyncRedis, message_id: str) -> None:
        """Unindex a status-index member whose row is gone — a row deleted from under the
        index rather than through :meth:`delete_record`."""
        logger.warning("conversations: record %r is indexed but has no row; unindexed", message_id)
        keys = self._record_keys(message_id)
        await eval_script(r, _UNINDEX_LUA, len(keys), *keys, message_id)

    async def pending_work(self) -> list[PendingWork]:
        """Every record the DELIVERY machine has unfinished work on — the listing behind
        the boot re-drive and the periodic sweep. Read from the status index, so it costs
        the work outstanding and not the whole retained keyspace. Terminal and intake
        records are not read (an intake record is the turn engine's to resolve); a corrupt
        row is logged and skipped rather than crashing the pass."""
        work: list[PendingWork] = []
        wanted = frozenset({DeliveryStatus.PENDING_DELIVERY, DeliveryStatus.PROVISIONAL})
        now = time.time()
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            for message_id in await self._indexed_ids(r, wanted, now):
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if not hashed:
                    await self._drop_orphan(r, message_id)
                    continue
                try:
                    # Parse the WHOLE row (content blob included), as list_by_status does: a
                    # row malformed anywhere is skipped here, not handed to a delivery that
                    # re-reads it unguarded and re-drives forever.
                    self._from_hash(hashed)
                    status = DeliveryStatus(hashed[_F_STATUS])
                    if status not in wanted:
                        # Moved on between the index read and this one; its new state is
                        # whoever wrote it to answer for.
                        continue
                    grace = hashed.get(_F_GRACE) or ""
                    found = PendingWork(
                        message_id=message_id,
                        delivery_status=status,
                        attempts=int(hashed[_F_ATTEMPTS]),
                        grace_deadline=float(grace) if grace else None,
                    )
                except (ValueError, KeyError):
                    # One unreadable row must not abort the pass, or every other record
                    # with unfinished work stays stranded behind it forever.
                    logger.warning(
                        "conversations: record %r is corrupt and was skipped in the delivery sweep", message_id
                    )
                    continue
                work.append(found)
        return work

    async def list_by_status(self, statuses: frozenset[DeliveryStatus]) -> list[ConversationRecord]:
        """Every record whose ``delivery_status`` is in ``statuses``, read from the status
        index. An unparseable row is logged and skipped rather than crashing the listing."""
        records: list[ConversationRecord] = []
        wanted = {status.value for status in statuses}
        now = time.time()
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            for message_id in await self._indexed_ids(r, statuses, now):
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if not hashed:
                    await self._drop_orphan(r, message_id)
                    continue
                if hashed.get(_F_STATUS) not in wanted:
                    continue
                try:
                    records.append(self._from_hash(hashed))
                except (ValueError, KeyError):
                    logger.warning(
                        "conversations: record %r is corrupt and was skipped in the status listing", message_id
                    )
        return records

    async def thread_has_live_intake(self, thread_id: str) -> bool:
        """Whether a turn is IN FLIGHT on ``thread_id`` — an ``accepted`` intake record still
        holding a LIVE lease. That turn's completion re-stamps the thread indexes and writes
        checkpoint state, so a delete racing it would half-forget the memory. The intake lease
        is the CROSS-WORKER liveness marker the re-drive trusts (an ``accepted`` record whose
        lease expiry is still ahead of now); the per-worker FIFO reservation cannot answer for
        a turn running on a sibling worker, so the lease is read here, never taken. Scanned
        off the ``accepted`` status index, which holds only the turns in flight fleet-wide."""
        now = time.time()
        accepted_key = self.settings.status_index_key(DeliveryStatus.ACCEPTED.value)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            for member in await awaited(r.zrange(accepted_key, 0, -1)):
                message_id = _member(member)
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if hashed.get(_F_STATUS) != DeliveryStatus.ACCEPTED.value:
                    continue
                try:
                    record_thread_id = json.loads(hashed[_F_DATA])["thread_id"]
                except (ValueError, KeyError):
                    logger.warning(
                        "conversations: record %r is corrupt and was skipped in the in-flight check", message_id
                    )
                    continue
                if record_thread_id != thread_id:
                    continue
                _token, sep, expiry = (hashed.get(_F_INTAKE) or "").partition(":")
                if sep and expiry and float(expiry) > now:
                    return True
        return False
