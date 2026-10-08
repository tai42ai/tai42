"""The per-status record index (keyspace 6): the delivery sweep, the status listing, and the intake check.

Each reads the index rather than the whole retained keyspace.
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass, field

from redis.asyncio import Redis as AsyncRedis
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.models import (
    TERMINAL_STATUSES,
    ConversationRecord,
    ConversationsIndexLayoutError,
    DeliveryStatus,
)
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


@dataclass(frozen=True)
class StatusListing:
    """A status listing with the rows it could read and a count of the ones it could not.

    ``unreadable`` counts the members omitted because their row was gone (an orphan the read
    unindexed) or unparseable, so a shorter list is never a silent cut.
    """

    items: list[ConversationRecord] = field(default_factory=list)
    unreadable: int = 0


def _text(member: str | bytes) -> str:
    return member.decode() if isinstance(member, bytes) else member


class RecordIndexMixin(RecordStoreBase):
    """Reads over the per-status record index (keyspace 6)."""

    async def _indexed_ids(self, r: AsyncRedis, statuses: frozenset[DeliveryStatus], now: float) -> list[str]:
        """The ``message_id``s indexed under ``statuses``.

        A terminal index's scores are row expiries, so its members whose row has expired out
        from under the index are dropped first; a live index's scores carry no expiry.
        """
        ids: list[str] = []
        for status in statuses:
            key = self.settings.status_index_key(status.value)
            if status in TERMINAL_STATUSES:
                await awaited(r.zremrangebyscore(key, "-inf", now))
            ids.extend(_text(member) for member in await awaited(r.zrange(key, 0, -1)))
        return ids

    async def _provisional_ids(self, r: AsyncRedis, *, due_only: bool, now: float) -> list[str]:
        """The ``provisional`` members: only those past their grace deadline when ``due_only``.

        The whole read checks the index layout: a member scored ``+inf`` was indexed without its
        grace deadline, a layout this code does not read, and raises.
        """
        key = self.settings.status_index_key(DeliveryStatus.PROVISIONAL.value)
        if due_only:
            return [_text(member) for member in await awaited(r.zrangebyscore(key, "-inf", now))]
        ids: list[str] = []
        for member, score in await awaited(r.zrange(key, 0, -1, withscores=True)):
            message_id = _text(member)
            if math.isinf(float(score)):
                raise ConversationsIndexLayoutError(
                    f"provisional record {message_id!r} is indexed without its grace deadline: the conversations "
                    "store predates this index layout; reset the conversations store"
                )
            ids.append(message_id)
        return ids

    async def _drop_orphan(self, r: AsyncRedis, message_id: str) -> None:
        """Unindex a status-index member whose row is gone.

        A row deleted from under the index rather than through :meth:`delete_record`.
        """
        logger.warning("conversations: record %r is indexed but has no row; unindexed", message_id)
        keys = self._record_keys(message_id)
        await eval_script(r, _UNINDEX_LUA, len(keys), *keys, message_id)

    async def pending_work(self, *, due_only: bool) -> list[PendingWork]:
        """The records the DELIVERY machine has unfinished work on.

        The listing behind the boot re-drive (``due_only=False``: every ``pending_delivery``
        and every ``provisional`` record, so the grace timers of those still inside their grace
        are rescheduled) and the periodic sweep (``due_only=True``: every ``pending_delivery``
        record, which a dead worker may have stranded, and only the ``provisional`` ones past
        their grace deadline). Read from the status index, so it costs the work outstanding and
        not the whole retained keyspace. Terminal and intake records are not read (an intake
        record is the turn engine's to resolve); an unrecoverable row is moved to the terminal
        ``failed`` state so it stops being re-enumerated every pass, never silently skipped.
        """
        work: list[PendingWork] = []
        unreadable_ids: list[str] = []
        wanted = frozenset({DeliveryStatus.PENDING_DELIVERY, DeliveryStatus.PROVISIONAL})
        now = time.time()
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            candidates = [
                *await self._indexed_ids(r, frozenset({DeliveryStatus.PENDING_DELIVERY}), now),
                *await self._provisional_ids(r, due_only=due_only, now=now),
            ]
            for message_id in candidates:
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if not hashed:
                    await self._drop_orphan(r, message_id)
                    continue
                try:
                    # Parse the WHOLE row (content blob included), as list_by_status does: a
                    # row malformed anywhere is caught here, not handed to a delivery that
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
                    # One unreadable row must not abort the pass, nor be re-skipped forever:
                    # move it to the terminal failed state so it leaves the sweep and appears
                    # on the admin failed listing.
                    logger.warning(
                        "conversations: record %r is corrupt; moving it to terminal failed in the delivery sweep",
                        message_id,
                    )
                    unreadable_ids.append(message_id)
                    continue
                work.append(found)
        for message_id in unreadable_ids:
            await self.mark_unreadable(message_id, now)
        return work

    async def list_by_status(self, statuses: frozenset[DeliveryStatus]) -> StatusListing:
        """Every record whose ``delivery_status`` is in ``statuses``, read from the status index.

        Returns the readable records and a count of the members it could not read — an orphan
        (indexed, row gone) it unindexed loudly, or an unparseable row it logged — so a shorter
        list is a truthful count, never a silent cut.
        """
        records: list[ConversationRecord] = []
        unreadable = 0
        wanted = {status.value for status in statuses}
        now = time.time()
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            for message_id in await self._indexed_ids(r, statuses, now):
                hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
                if not hashed:
                    await self._drop_orphan(r, message_id)
                    unreadable += 1
                    continue
                if hashed.get(_F_STATUS) not in wanted:
                    continue
                try:
                    records.append(self._from_hash(hashed))
                except (ValueError, KeyError):
                    logger.warning(
                        "conversations: record %r is corrupt and was skipped in the status listing", message_id
                    )
                    unreadable += 1
        return StatusListing(items=records, unreadable=unreadable)

    async def thread_has_live_intake(self, thread_id: str) -> bool:
        """Whether a turn is IN FLIGHT on ``thread_id`` — an ``accepted`` intake record still holding a LIVE lease.

        That turn's completion re-stamps the thread indexes and writes
        checkpoint state, so a delete racing it would half-forget the memory. The intake lease
        is the CROSS-WORKER liveness marker the re-drive trusts (an ``accepted`` record whose
        lease expiry is still ahead of now); the per-worker FIFO reservation cannot answer for
        a turn running on a sibling worker, so the lease is read here, never taken. Scanned
        off the ``accepted`` status index, which holds only the turns in flight fleet-wide.
        """
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
