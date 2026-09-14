"""The answer record itself (keyspace 2): the key layout every mutating script takes, the
content codec, and the create / turn-completion / intake-lease / delete / read writes."""

from __future__ import annotations

import json
import logging
import time

from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.models import TERMINAL_STATUSES, ConversationRecord, DeliveryStatus
from tai42_skeleton.conversations.record_scripts import (
    _CLAIM_INTAKE_LUA,
    _COMPLETE_SILENT_LUA,
    _COMPLETE_TURN_LUA,
    _CREATE_LUA,
    _DELETE_LUA,
    _F_ATTEMPTS,
    _F_DATA,
    _F_OUTBOUND,
    _F_STATUS,
    _F_UPDATED,
    _INDEXED_STATUSES,
    _NO_EXPIRY_SCORE,
)
from tai42_skeleton.conversations.record_store_base import RecordStoreBase
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)


class RecordWriteMixin(RecordStoreBase):
    """The answer record's key layout, codec and mutating writes (keyspace 2)."""

    # -- the key layout every record-mutating script takes --------------------

    def _record_keys(
        self,
        message_id: str,
        target: DeliveryStatus | None = None,
        thread: ConversationRecord | None = None,
        *,
        route_row: bool = False,
    ) -> list[str]:
        """``[record key, every status index, the target status index, the record's two
        thread indexes, the routing row]``. ``target`` is omitted only by a script that
        writes no status; ``thread`` only by one that touches no thread index;
        ``route_row`` is taken only by the create, which decides against it whether the
        route still routes."""
        keys = [self.settings.record_key(message_id)]
        keys.extend(self.settings.status_index_key(status.value) for status in _INDEXED_STATUSES)
        if target is not None:
            keys.append(self.settings.status_index_key(target.value))
        if thread is not None:
            keys.append(self.settings.thread_index_key(thread.route_name, thread.thread_id))
            keys.append(self.settings.route_threads_key(thread.route_name))
            if route_row:
                keys.append(self.settings.route_key(thread.route_name))
        return keys

    def _index_score(self, status: DeliveryStatus, now: float) -> str:
        """The index member's expiry: a terminal row's member is swept with the row it
        names, a live one's is never swept."""
        if status not in TERMINAL_STATUSES:
            return _NO_EXPIRY_SCORE
        return str(now + self.settings.answer_retention_ttl_seconds)

    # -- answer record (keyspace 2) ------------------------------------------

    def _content_blob(self, record: ConversationRecord) -> str:
        """The record's content JSON — every field except the delivery-control ones, which
        live in their own hash fields.

        ``allow_nan=False``: a non-finite float renders as bare ``Infinity``/``NaN``, which
        is not JSON and which no standard parser reads back, so the row would persist
        unreadable. It raises here, at the write, instead."""
        content = record.model_dump(mode="json")
        for control in ("delivery_status", "outbound_message_ids", "attempts", "updated_at"):
            content.pop(control, None)
        return json.dumps(content, allow_nan=False)

    async def create_record(self, record: ConversationRecord, *, intake_token: str | None = None) -> None:
        """Persist a freshly minted record in the state it carries (always a create — the
        ``message_id`` is a fresh uuid4, or a caller's stable idempotency id its caller
        dedupes on before calling). A non-terminal record carries NO expiry until it
        reaches a terminal state; one created already terminal gets the retention TTL.

        An ``accepted`` record REQUIRES ``intake_token`` and is created already holding that
        worker's intake lease; any other state requires none.

        The thread indexes are written only while the record's route still routes: a door
        resolves its route a round trip before this write, and a delete completing in that
        window has already reclaimed both indexes. The record still stands (its delivery is
        the sender's to finish), but no transcript names it — logged loudly, because a
        message answered against a route that vanished mid-turn is an operator's business."""
        if (record.delivery_status is DeliveryStatus.ACCEPTED) is not (intake_token is not None):
            raise ValueError(
                "an accepted record is created holding an intake lease and any other state without one; got "
                f"{record.delivery_status.value!r} with intake_token={intake_token!r}"
            )
        now = time.time()
        terminal = record.delivery_status in TERMINAL_STATUSES
        ttl_ms = self.settings.answer_retention_ttl_seconds * 1000 if terminal else ""
        lease_until = now + self.settings.intake_claim_lease_seconds
        keys = self._record_keys(record.message_id, record.delivery_status, record, route_row=True)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            indexed = int(
                await eval_script(
                    r,
                    _CREATE_LUA,
                    len(keys),
                    *keys,
                    self._content_blob(record),
                    record.delivery_status.value,
                    json.dumps(record.outbound_message_ids),
                    record.attempts,
                    record.updated_at,
                    ttl_ms,
                    f"{intake_token}:{lease_until}" if intake_token is not None else "",
                    record.message_id,
                    self._index_score(record.delivery_status, now),
                    record.thread_id,
                    record.created_at,
                )
            )
        if not indexed:
            logger.warning(
                "conversations: route %r stopped routing while record %r was being accepted; the record stands "
                "but its thread indexes were not written, so no transcript names it",
                record.route_name,
                record.message_id,
            )

    async def complete_turn(self, record: ConversationRecord) -> int:
        """Move an intake record from ``accepted`` to ``pending_delivery`` carrying its
        turn's outcome: 1 transitioned, 0 no longer at intake, -1 gone. Guarded on the
        current status, so a late turn and a re-drive cannot both write an outcome."""
        if record.delivery_status is not DeliveryStatus.PENDING_DELIVERY:
            raise ValueError(f"complete_turn writes a pending_delivery record, got {record.delivery_status.value!r}")
        keys = self._record_keys(record.message_id, DeliveryStatus.PENDING_DELIVERY, record)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _COMPLETE_TURN_LUA,
                    len(keys),
                    *keys,
                    self._content_blob(record),
                    record.updated_at,
                    record.message_id,
                    self._index_score(DeliveryStatus.PENDING_DELIVERY, record.updated_at),
                    record.thread_id,
                )
            )

    async def complete_silent(self, record: ConversationRecord) -> int:
        """Move an intake record from ``accepted`` straight to terminal ``silent`` — a tool
        turn that produced no reply — applying the retention TTL: 1 transitioned, 0 no
        longer at intake, -1 gone. Guarded on the current status, so a late turn and a
        re-drive cannot both write an outcome."""
        if record.delivery_status is not DeliveryStatus.SILENT:
            raise ValueError(f"complete_silent writes a silent record, got {record.delivery_status.value!r}")
        # One fresh ``now`` feeds both the index member's expiry score and the ``updated_at``
        # field, so the member's score tracks the row's own TTL clock — the sibling terminal
        # writes' idiom.
        now = time.time()
        keys = self._record_keys(record.message_id, DeliveryStatus.SILENT, record)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _COMPLETE_SILENT_LUA,
                    len(keys),
                    *keys,
                    self._content_blob(record),
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    record.message_id,
                    self._index_score(DeliveryStatus.SILENT, now),
                    record.thread_id,
                )
            )

    async def claim_intake(self, message_id: str, now: float, token: str, lease_seconds: float) -> int:
        """Take (or refresh) the intake lease on ``message_id`` under ``token``, leased for
        ``lease_seconds``: 1 when held, 0 when a DIFFERENT worker's lease is still live, -1
        when the record is gone, -2 when it has left intake. The worker running the turn
        refreshes its own lease; a re-drive may adopt only a LAPSED one, so a live turn is
        never reaped."""
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _CLAIM_INTAKE_LUA,
                    1,
                    self.settings.record_key(message_id),
                    now,
                    lease_seconds,
                    token,
                )
            )

    async def delete_record(self, record: ConversationRecord) -> bool:
        """Delete a record and its index membership outright, returning whether one was
        removed — the abort path for an accept that lost the inbound claim and so owns
        nothing. Takes the record, not its id: the thread it is indexed under is named by
        the record alone."""
        keys = self._record_keys(record.message_id, thread=record)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return bool(await eval_script(r, _DELETE_LUA, len(keys), *keys, record.message_id, record.thread_id))

    async def get_record(self, message_id: str) -> ConversationRecord | None:
        """The record for ``message_id``, or ``None`` when no such record exists."""
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            hashed = await awaited(r.hgetall(self.settings.record_key(message_id)))
        if not hashed:
            return None
        return self._from_hash(hashed)

    def _from_hash(self, hashed: dict[str, str]) -> ConversationRecord:
        data = json.loads(hashed[_F_DATA])
        data[_F_STATUS] = hashed[_F_STATUS]
        data["outbound_message_ids"] = json.loads(hashed[_F_OUTBOUND])
        data[_F_ATTEMPTS] = int(hashed[_F_ATTEMPTS])
        data[_F_UPDATED] = float(hashed[_F_UPDATED])
        return ConversationRecord.model_validate(data)
