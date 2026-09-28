"""The delivery-state machine over an answer record.

The exactly-once send lease and the provisional / delivered / failed / receipt transitions,
plus the outbound reverse index (keyspace 3).
"""

from __future__ import annotations

import json
import logging

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
    _UNREADABLE_LUA,
)
from tai42_skeleton.conversations.record_store_base import RecordStoreBase
from tai42_skeleton.utils.redis_typing import awaited, eval_script

logger = logging.getLogger(__name__)


class RecordDeliveryMixin(RecordStoreBase):
    """The send lease, the delivery-state transitions, and the outbound reverse index."""

    async def claim_delivery(self, message_id: str, now: float, token: str, lease_seconds: float) -> int:
        """Take (or refresh) the exactly-once delivery lease on ``message_id`` under ``token`` for ``lease_seconds``.

        Returns 1 when won, 0 when the record is already sent (provisional) or terminal or a
        different worker holds a live lease, -1 when the record is gone, -2 when it is still at
        intake and carries no answer. Only a ``pending_delivery`` record is claimable for a send.
        The token holder re-claiming extends its own lease; a different token waits for expiry.
        """
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
        """Move ``message_id`` to ``provisional`` awaiting an async delivery receipt or grace expiry.

        Returns 1 transitioned, 0 already terminal, -1 gone, -3 a different worker's live lease. A
        receipt that arrived during the send is applied in this same step, straight to its terminal
        (skipping ``provisional``): 2 when the parked receipt was ``delivered``, 3 when it was
        ``failed``. ``token`` is the delivery lease this caller holds.
        """
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
                    self.settings.answer_retention_ttl_seconds * 1000,
                    self._index_score(DeliveryStatus.DELIVERED, now),
                )
            )

    async def mark_delivered(
        self, message_id: str, outbound_ids: list[str], attempts: int, now: float, token: str
    ) -> int:
        """Terminal delivered write with the retention TTL.

        Returns 1 transitioned, 0 already delivered, -1 gone, -2 already failed, -3 a different
        worker's live lease. ``token`` is the delivery lease this caller holds.
        """
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
        """Terminal failed write with the retention TTL.

        Returns 1 transitioned, 0 already failed, -1 gone, -2 the send already completed
        (delivered/shed/provisional), -3 a different worker's live lease. ``token`` is the delivery
        lease this caller holds.

        A sender-side failure that reaches this real terminal beats a receipt only staged against
        an in-flight record: the Lua drops the staged ``pending_receipt`` in the same step and
        signals it with code 2, which this wrapper logs at WARNING (the drop is operator-visible)
        and remaps to 1 — the record's failed state is the truthful outcome, so the drop is a
        defined, logged next state, never an error hidden.
        """
        keys = self._record_keys(message_id, DeliveryStatus.FAILED)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            result = int(
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
        if result == 2:
            logger.warning(
                "conversations: record %r failed while a receipt was parked; the parked receipt is dropped "
                "— the send-side terminal is the outcome",
                message_id,
            )
            return 1
        return result

    async def mark_unreadable(self, message_id: str, now: float) -> int:
        """Move an unrecoverable sweep row to the terminal ``failed`` state, applying the retention TTL.

        The sweep-owned terminal primitive: it holds no delivery lease, so it threads no token or
        ``attempts`` and reads a corrupt row's status opaquely. Returns 1 transitioned, 0 already
        failed, -1 the row is gone, -2 the send already completed (delivered/shed), -3 a DIFFERENT
        worker holds a live delivery lease — in which case the row is left to that holder (it finishes
        it, or its lease lapses and the next sweep transitions it) and the -3 is logged at WARNING, a
        defined next state, never a swallow.

        A corrupt row that carried a staged receipt has it dropped in the same step, signalled by
        code 2, which this wrapper logs at WARNING and remaps to 1: first terminal wins, the sweep's
        failed state is the truthful outcome and the drop is operator-visible, never hidden.
        """
        keys = self._record_keys(message_id, DeliveryStatus.FAILED)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            result = int(
                await eval_script(
                    r,
                    _UNREADABLE_LUA,
                    len(keys),
                    *keys,
                    now,
                    self.settings.answer_retention_ttl_seconds * 1000,
                    message_id,
                    self._index_score(DeliveryStatus.FAILED, now),
                )
            )
        if result == 2:
            logger.warning(
                "conversations: record %r unreadable while a receipt was parked; the parked receipt is dropped "
                "— the send-side terminal is the outcome",
                message_id,
            )
            return 1
        if result == -3:
            logger.warning(
                "conversations: record %r is under a live foreign delivery lease; left to its holder this sweep",
                message_id,
            )
        return result

    async def ingest_receipt(self, message_id: str, receipt: DeliveryReceipt, now: float) -> int:
        """Ingest an out-of-band receipt for an outbound message.

        Returns 1 transitioned (a ``provisional`` record settled), 2 parked (the record's send is
        still in flight — the receipt is staged and applied by the completing provisional write), 0
        already in the receipt's terminal state or re-parked to the same target, -1 gone, -2 a
        conflicting terminal or a different target already parked, -3 the id resolved to a record in
        a state that never sent an answer.
        """
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
        """Map each outbound provider id back to ``message_id`` so an out-of-band receipt resolves to the record.

        Written with the retention TTL, so it is swept with it.
        """
        if not outbound_ids:
            return
        ttl = self.settings.answer_retention_ttl_seconds
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            for outbound_id in outbound_ids:
                await awaited(r.set(self.settings.outbound_index_key(channel, outbound_id), message_id, ex=ttl))

    async def resolve_outbound(self, channel: str, outbound_id: str) -> str | None:
        """The ``message_id`` an outbound provider id maps to, or ``None`` when the index holds no such id.

        An unknown id, or one already swept.
        """
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.get(self.settings.outbound_index_key(channel, outbound_id)))
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw
