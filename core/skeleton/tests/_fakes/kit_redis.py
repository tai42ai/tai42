"""A ``fakeredis`` async client with the kit client's pub/sub.

Production Redis handles are :class:`~tai42_kit.clients.impl.redis.KitAsyncRedis`, whose
``pubsub(on_reconnect=...)`` runs a hook after each re-subscribe. :class:`KitFakeRedis`
keeps ``fakeredis``'s in-process server and takes the kit client's ``pubsub``, so code
that passes ``on_reconnect`` runs against the fake as it does against the real client.
"""

from __future__ import annotations

from fakeredis import aioredis
from tai42_kit.clients.impl.redis import KitAsyncRedis


class KitFakeRedis(aioredis.FakeRedis, KitAsyncRedis):
    """``fakeredis.aioredis.FakeRedis`` whose ``pubsub`` is the kit client's."""
