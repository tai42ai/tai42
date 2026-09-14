"""The delivery-state machine over an answer record: the exactly-once send lease and the
provisional / delivered / failed / receipt transitions, plus the outbound reverse index
(keyspace 3)."""

from __future__ import annotations

import json

from tai42_contract.conversations import DeliveryReceipt
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.models import DeliveryStatus
from tai42_skeleton.conversations.record_scripts import (
    _CLAIM_DELIVERY_LUA,
    _DELIVERED_LUA,
    _F_ATTEMPTS,
    _FAILED_LUA,
    _PROVISIONAL_LUA,
    _RECEIPT_LUA,
)
from tai42_skeleton.conversations.record_store_base import RecordStoreBase
from tai42_skeleton.utils.redis_typing import awaited, eval_script


class RecordDeliveryMixin(RecordStoreBase):
    """The send lease, the delivery-state transitions, and the outbound reverse index."""

    async def claim_delivery(self, message_id: str, now: float, token: str, lease_seconds: float) -> int:
        """Take (or refresh) the exactly-once delivery lease on ``message_id`` under
        ``token``, leased for ``lease_seconds``: 1 when won, 0 when the record is already
        sent (provisional) or terminal or a different worker holds a live lease, -1 when the
        record is gone, -2 when it is still at intake and carries no answer. Only a
        ``pending_delivery`` record is claimable for a send. The token holder re-claiming
        extends its own lease; a different token waits for expiry."""
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _CLAIM_DELIVERY_LUA,
                    1,
                    self.settings.record_key(message_id),
                    now,
                    lease_seconds,
                    token,
                )
            )

    async def bump_attempt(self, message_id: str) -> int:
        """Increment and return the record's attempt count — one per send attempt."""
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(await awaited(r.hincrby(self.settings.record_key(message_id), _F_ATTEMPTS, 1)))

    async def mark_provisional(
        self, message_id: str, outbound_ids: list[str], attempts: int, now: float, token: str
    ) -> int:
        """Move ``message_id`` to ``provisional`` awaiting an async delivery receipt or
        grace expiry: 1 transitioned, 0 already terminal, -1 gone, -3 a different worker's
        live lease. ``token`` is the delivery lease this caller holds."""
        grace_deadline = now + self.settings.delivery_grace_seconds
        keys = self._record_keys(message_id, DeliveryStatus.PROVISIONAL)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _PROVISIONAL_LUA,
                    len(keys),
                    *keys,
                    json.dumps(outbound_ids),
                    attempts,
                    grace_deadline,
                    now,
                    token,
                    message_id,
                    self._index_score(DeliveryStatus.PROVISIONAL, now),
                )
            )

    async def mark_delivered(
        self, message_id: str, outbound_ids: list[str], attempts: int, now: float, token: str
    ) -> int:
        """Terminal delivered write with the retention TTL: 1 transitioned, 0 already
        delivered, -1 gone, -2 already failed, -3 a different worker's live lease.
        ``token`` is the delivery lease this caller holds."""
        keys = self._record_keys(message_id, DeliveryStatus.DELIVERED)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _DELIVERED_LUA,
                    len(keys),
                    *keys,
                    json.dumps(outbound_ids),
                    attempts,
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    token,
                    message_id,
                    self._index_score(DeliveryStatus.DELIVERED, now),
                )
            )

    async def mark_failed(self, message_id: str, attempts: int, now: float, token: str) -> int:
        """Terminal failed write with the retention TTL: 1 transitioned, 0 already failed,
        -1 gone, -2 the send already completed (delivered/shed/provisional), -3 a different
        worker's live lease. ``token`` is the delivery lease this caller holds."""
        keys = self._record_keys(message_id, DeliveryStatus.FAILED)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _FAILED_LUA,
                    len(keys),
                    *keys,
                    attempts,
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    token,
                    message_id,
                    self._index_score(DeliveryStatus.FAILED, now),
                )
            )

    async def ingest_receipt(self, message_id: str, receipt: DeliveryReceipt, now: float) -> int:
        """Ingest an out-of-band receipt against a fully sent (``provisional``) record: 1
        transitioned, 0 already in the receipt's terminal state, -1 gone, -2 a conflicting
        terminal state already recorded, -3 the record's send has not finished."""
        target = DeliveryStatus.DELIVERED if receipt is DeliveryReceipt.DELIVERED else DeliveryStatus.FAILED
        keys = self._record_keys(message_id, target)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            return int(
                await eval_script(
                    r,
                    _RECEIPT_LUA,
                    len(keys),
                    *keys,
                    target.value,
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    message_id,
                    self._index_score(target, now),
                )
            )

    # -- outbound reverse index (keyspace 3) ---------------------------------

    async def index_outbound(self, channel: str, outbound_ids: list[str], message_id: str) -> None:
        """Map each outbound provider id back to ``message_id`` so an out-of-band receipt
        resolves to the record. Written with the retention TTL, so it is swept with it."""
        if not outbound_ids:
            return
        ttl = self.settings.answer_retention_ttl_seconds
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            for outbound_id in outbound_ids:
                await awaited(r.set(self.settings.outbound_index_key(channel, outbound_id), message_id, ex=ttl))

    async def resolve_outbound(self, channel: str, outbound_id: str) -> str | None:
        """The ``message_id`` an outbound provider id maps to, or ``None`` when the index
        holds no such id (an unknown id, or one already swept)."""
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.get(self.settings.outbound_index_key(channel, outbound_id)))
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw
