"""The arq enqueue connection: an :class:`~arq.connections.ArqRedis` facade over the kit's pooled Redis client.

arq-native operations (enqueue, ``Job`` status/result, schedule hashes) lease the kit's
pooled async Redis client for this backend's connection settings and drive arq through an
``ArqRedis`` built over that client's connection pool, configured with this backend's JSON
job (de)serializers and queue name. The pool is the kit's: one per client epoch, drained
when the serving generation that leased it retires, its connections opened from an empty
context. The facade owns nothing and is built per lease. An unreachable Redis fails loudly
at first use, as for every kit-pooled client, after the kit seam's logged connection retry
(``ARQ_RETRY_ATTEMPTS``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from arq import ArqRedis
from redis.asyncio import ConnectionPool
from redis.asyncio import Redis as AsyncRedis
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_backend_arq.settings import arq_settings, job_deserializer, job_serializer


def enqueue_client_kwargs() -> dict[str, Any]:
    """The kit Redis client kwargs for this backend's connection settings, read as bytes.

    The URL carries the host, port, database, credentials and TLS (``rediss://``); the
    connection cap, the socket timeouts and the connection retry ride beside it, all from
    :class:`~tai42_backend_arq.settings.ArqRedisSettings`. Responses stay bytes, the shape
    arq reads.
    """
    return {**arq_settings().redis.client_kwargs(), "decode_responses": False}


def arq_redis_over(client: AsyncRedis, *, queue_name: str) -> ArqRedis:
    """An ``ArqRedis`` over ``client``'s connection pool, with this backend's job (de)serializers.

    The pool's connection kwargs carry the kit's retry, so every arq command runs under it.
    """
    connection_pool = client.connection_pool
    if not isinstance(connection_pool, ConnectionPool):
        raise TypeError(f"the kit Redis client's pool is {type(connection_pool).__name__}, not an asyncio pool")
    return ArqRedis(
        pool_or_conn=connection_pool,
        job_serializer=job_serializer,
        job_deserializer=job_deserializer,
        default_queue_name=queue_name,
    )


@asynccontextmanager
async def arq_connection() -> AsyncIterator[Any]:
    """Lease the kit's pooled Redis client and yield an ``ArqRedis`` over its connection pool.

    Yielded as ``Any``: redis-py types each command's return as the union of its sync and
    async shapes, while this async client's commands always return an awaitable.
    """
    async with client_ctx(RedisClient, **enqueue_client_kwargs()) as client:
        yield arq_redis_over(client, queue_name=arq_settings().queue_name)
