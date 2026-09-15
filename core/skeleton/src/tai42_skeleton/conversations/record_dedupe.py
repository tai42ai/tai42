"""Inbound dedupe (keyspace 1): the once-only channel and event claim markers.

A redelivery fast path reads them and concurrent accepts arbitrate through them.
"""

from __future__ import annotations

from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.record_scripts import _CLAIM_INBOUND_LUA
from tai42_skeleton.conversations.record_store_base import RecordStoreBase
from tai42_skeleton.utils.redis_typing import awaited, eval_script


class RecordDedupeMixin(RecordStoreBase):
    """The channel and event inbound-dedupe markers (keyspace 1)."""

    async def _dedupe_owner(self, key: str) -> str | None:
        """Return the ``message_id`` owning a dedupe marker key, or ``None`` when unclaimed.

        The shared read behind every door's redelivery fast path. It claims nothing, so
        :meth:`_claim_dedupe` stays the authority concurrent accepts arbitrate through.
        """
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            owner = await awaited(r.get(key))
        if owner is None:
            return None
        return owner.decode() if isinstance(owner, bytes) else owner

    async def _claim_dedupe(self, key: str, message_id: str) -> str:
        """Atomically get-or-set a dedupe marker for ``message_id`` and return the ``message_id`` that OWNS the key.

        Runs under the shared ``inbound_dedupe_ttl_seconds``; the owner is the passed one on
        a fresh claim, the prior turn's on a redelivery. Every dedupe family (channel and
        event) runs the SAME ``_CLAIM_INBOUND_LUA`` through here, so their arbitration is
        byte-identical. The claim has no release path, so the caller must take it only once
        its record is durably persisted, and discard that record on a loss.
        """
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            owner = await eval_script(
                r, _CLAIM_INBOUND_LUA, 1, key, message_id, self.settings.inbound_dedupe_ttl_seconds
            )
        return owner.decode() if isinstance(owner, bytes) else owner

    async def get_inbound_owner(self, channel: str, provider_message_id: str) -> str | None:
        """Return the ``message_id`` owning ``(channel, provider_message_id)``, or ``None`` — the channel fast path."""
        return await self._dedupe_owner(self.settings.dedupe_key(channel, provider_message_id))

    async def claim_inbound(self, channel: str, provider_message_id: str, message_id: str) -> str:
        """Claim ``(channel, provider_message_id)`` for ``message_id`` in the channel dedupe family.

        Returns the owning ``message_id``.
        """
        return await self._claim_dedupe(self.settings.dedupe_key(channel, provider_message_id), message_id)

    async def get_event_owner(self, route_name: str, event_id: str) -> str | None:
        """Return the ``message_id`` owning ``(route_name, event_id)``, or ``None`` when unclaimed.

        The event door's redelivery fast path, over the event dedupe family.
        """
        return await self._dedupe_owner(self.settings.event_dedupe_key(route_name, event_id))

    async def claim_event(self, route_name: str, event_id: str, message_id: str) -> str:
        """Claim ``(route_name, event_id)`` for ``message_id`` in the event dedupe family; return the owner.

        Runs the SAME claim as :meth:`claim_inbound` over its own key family, so a channel
        dedupe and an event dedupe of the same id are distinct markers.
        """
        return await self._claim_dedupe(self.settings.event_dedupe_key(route_name, event_id), message_id)
