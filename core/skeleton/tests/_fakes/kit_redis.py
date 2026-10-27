"""A ``fakeredis`` async client that is the kit client over kit connections.

Production Redis handles are :class:`~tai42_kit.clients.impl.redis.KitAsyncRedis`, whose
``pubsub(on_reconnect=...)`` runs a hook after each re-subscribe, over kit async
connections, which connect under their retry and run that hook unretried.
:class:`KitFakeRedis` keeps ``fakeredis``'s in-process server and takes the kit client's
``pubsub``, and its connections are the kit async connection class over ``fakeredis``'s
connection, so code that passes ``on_reconnect`` runs against the fake as it does against
the real client.
"""

from __future__ import annotations

from typing import Any

from fakeredis import aioredis
from tai42_kit.clients.impl.redis import KitAsyncRedis
from tai42_kit.clients.impl.redis_connections import kit_async_connection_class

# The kit async connection class over ``fakeredis``'s async connection.
KitFakeConnection = kit_async_connection_class(aioredis.FakeAsyncRedisConnection)


class KitFakeRedis(aioredis.FakeRedis, KitAsyncRedis):
    """``fakeredis.aioredis.FakeRedis`` whose ``pubsub`` is the kit client's, over :data:`KitFakeConnection`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """``fakeredis``'s client from ``args`` / ``kwargs``, its connections :data:`KitFakeConnection`."""
        kwargs.setdefault("connection_class", KitFakeConnection)
        super().__init__(*args, **kwargs)

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> KitFakeRedis:
        """``fakeredis``'s client for ``url``, its connections :data:`KitFakeConnection`."""
        kwargs.setdefault("connection_class", KitFakeConnection)
        client = super().from_url(url, **kwargs)
        assert isinstance(client, cls)
        return client
