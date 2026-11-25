"""The receipt flags and the per-participant last-outbound marker that order sends to one participant.

A receipt flag says one outbound provider id was reported delivered or failed; the receipt door writes
it before anything resolves the id, so a send on any process holding for that message reads it. The
marker names the last message sent to one participant conversation and when, so the next send there,
from any door, holds for its receipt. The marker lives one receipt wait; a flag lives two, unless
its record is still sending, whose receipt script and outbound index keep it for the answer retention.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

from tai42_contract.conversations import DeliveryReceipt
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.record_store_base import RecordStoreBase
from tai42_skeleton.utils.redis_typing import awaited, eval_script

# Deletes the marker only while it still names the message that was waited for, so a marker a newer
# send wrote meanwhile stands.
_CLEAR_PREVIOUS_OUTBOUND_LUA = """
local raw = redis.call('GET', KEYS[1])
if raw and cjson.decode(raw)['id'] == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


@dataclass(frozen=True)
class ReceiptFlag:
    """A provider's delivery receipt for one outbound id: its status and the provider's error detail."""

    status: DeliveryReceipt
    errors: Any


@dataclass(frozen=True)
class PreviousOutbound:
    """The last message sent to one participant conversation: its provider id and the time it was sent."""

    provider_message_id: str
    sent_at: float


def _text(raw: str | bytes) -> str:
    return raw.decode() if isinstance(raw, bytes) else raw


class RecordOutboundOrderMixin(RecordStoreBase):
    """The receipt flags and the last-outbound marker the order of sends to one participant reads."""

    def _receipt_flag_ttl_ms(self) -> int:
        # Two receipt waits cover a live hold on any process: a receipt can land before the send's own
        # return, and a waiter's deadline is ``sent_at + wait``. A receipt landing while its record's send
        # is in flight is kept for the answer retention instead, by the record's receipt script or the
        # outbound index, because another worker may resume that send until the record leaves
        # ``pending_delivery``.
        return math.ceil(2 * self.settings.delivery_receipt_wait_seconds * 1000)

    async def signal_receipt(
        self, channel: str, provider_message_id: str, status: DeliveryReceipt, errors: Any = None
    ) -> None:
        """Flag ``provider_message_id`` on ``channel`` as reported ``status``, with the provider's ``errors``."""
        key = self.settings.keyspace.receipt_flag_key(channel, provider_message_id)
        value = json.dumps({"status": status.value, "errors": errors}, default=str)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.set(key, value, px=self._receipt_flag_ttl_ms()))

    async def receipt_flags(self, ids: list[tuple[str, str]]) -> list[ReceiptFlag | None]:
        """The receipt flag of each ``(channel, provider_message_id)``, in order; ``None`` where none landed."""
        if not ids:
            return []
        keys = [self.settings.keyspace.receipt_flag_key(channel, pid) for channel, pid in ids]
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            raws = await awaited(r.mget(keys))
        flags: list[ReceiptFlag | None] = []
        for raw in raws:
            if raw is None:
                flags.append(None)
                continue
            payload = json.loads(_text(raw))
            flags.append(ReceiptFlag(status=DeliveryReceipt(payload["status"]), errors=payload["errors"]))
        return flags

    async def previous_outbound(self, channel: str, our_identity: str, client_address: str) -> PreviousOutbound | None:
        """The last message sent to the participant conversation, or ``None`` when no send there is still held for."""
        key = self.settings.keyspace.outbound_order_key(channel, our_identity, client_address)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.get(key))
        if raw is None:
            return None
        payload = json.loads(_text(raw))
        return PreviousOutbound(provider_message_id=payload["id"], sent_at=float(payload["sent_at"]))

    async def set_previous_outbound(
        self, channel: str, our_identity: str, client_address: str, provider_message_id: str, sent_at: float
    ) -> None:
        """Name ``provider_message_id``, sent at ``sent_at``, as the participant conversation's last message."""
        key = self.settings.keyspace.outbound_order_key(channel, our_identity, client_address)
        value = json.dumps({"id": provider_message_id, "sent_at": sent_at})
        ttl_ms = math.ceil(self.settings.delivery_receipt_wait_seconds) * 1000
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.set(key, value, px=ttl_ms))

    async def clear_previous_outbound(
        self, channel: str, our_identity: str, client_address: str, provider_message_id: str
    ) -> None:
        """Drop the participant conversation's marker while it still names ``provider_message_id``."""
        key = self.settings.keyspace.outbound_order_key(channel, our_identity, client_address)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            await eval_script(r, _CLEAR_PREVIOUS_OUTBOUND_LUA, 1, key, provider_message_id)


__all__ = ["PreviousOutbound", "ReceiptFlag", "RecordOutboundOrderMixin"]
