"""Owed first-contact greeting (keyspace 9): the greeting a thread still owes its next reply.

A first-contact greeting is minted the moment a thread's person is created, but the turn that
mints it can be superseded or cancelled in favour of a newer message before it delivers. So the
rendered greeting is parked here, per thread, and consumed by the first delivering MESSAGE turn on
the thread — the greeting rides that successor turn instead of being dropped. An event turn carries
no greeting and leaves it owed.

Consumption is two steps, not one atomic pop: the delivering turn READs the greeting (GET) while
building its outcome, and BURNs it (DEL) only after that turn's guarded durable persist has
succeeded. Exactly-once delivery then rests on the intake guard — only the one turn that wins the
guarded persist reaches the burn, so no two turns ever both deliver-and-burn. A worker that dies
between the persist and the burn leaves the greeting owed, so the next delivering turn re-reads and
re-delivers it once: the trade for never dropping a greeting whose resolver lost its guard is a
possible single re-delivery across a crash in that narrow window.
"""

from __future__ import annotations

from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.conversations import records as _records
from tai42_skeleton.conversations.record_store_base import RecordStoreBase
from tai42_skeleton.utils.redis_typing import awaited


class RecordGreetingMixin(RecordStoreBase):
    """The per-thread owed first-contact greeting (keyspace 9)."""

    async def record_owed_greeting(self, thread_id: str, greeting: str) -> None:
        """Park ``greeting`` as owed on ``thread_id`` until a turn delivers it.

        Written when a first-contact greeting is minted, before the turn runs, so a supersede or
        cancel that reaches the turn before it delivers leaves the greeting standing for the
        successor to consume. TTL'd to the pair-code lifetime: a greeting may carry a
        ``{pairing_code}``, and delivering it after that code expired would present a dead code, so
        the owed greeting never outlives the window its code is live for.
        """
        key = self.settings.owed_greeting_key(thread_id)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.set(key, greeting, px=self.settings.pair_code_ttl_seconds * 1000))

    async def read_owed_greeting(self, thread_id: str) -> str | None:
        """Return the greeting owed on ``thread_id`` WITHOUT consuming it, or ``None`` when none is owed.

        A plain ``GET``: the greeting stays parked, so a turn whose outcome never persists (its guard
        lost to a re-drive, or the turn superseded) leaves it owed for the successor. The delivering
        turn burns it with :meth:`burn_owed_greeting` only after its persist succeeds.
        """
        key = self.settings.owed_greeting_key(thread_id)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            raw = await awaited(r.get(key))
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else raw

    async def burn_owed_greeting(self, thread_id: str) -> None:
        """Delete the owed greeting on ``thread_id`` — the turn that delivered it has persisted.

        A plain ``DEL``, run only after the delivering turn's guarded persist succeeded, so exactly
        one turn ever both delivers and burns; every later turn reads ``None`` and prepends nothing.
        """
        key = self.settings.owed_greeting_key(thread_id)
        async with _records.client_ctx(RedisClient, self.settings.redis) as r:
            await awaited(r.delete(key))
