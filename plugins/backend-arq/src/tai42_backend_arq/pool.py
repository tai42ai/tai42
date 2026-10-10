"""The arq enqueue connection: an :class:`~arq.connections.ArqRedis` facade over the kit's pooled Redis client.

arq-native operations (enqueue, ``Job`` status/result, schedule hashes) lease the kit's
pooled async Redis client for this backend's connection settings and drive arq through an
``ArqRedis`` built over that client's connection pool, configured with this backend's JSON
job (de)serializers and queue name. The pool is the kit's: one per client epoch, drained
when the serving generation that leased it retires, its connections opened from an empty
context. The facade owns nothing and is built per lease. An unreachable Redis fails loudly
at first use, as for every kit-pooled client, through the client's own command retry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from arq import ArqRedis
from redis.asyncio import ConnectionPool
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient

from tai42_backend_arq.settings import arq_settings, job_deserializer, job_serializer


def enqueue_client_kwargs() -> dict[str, Any]:
    """The kit Redis client kwargs for this backend's connection settings.

    The URL carries the host, port, database, credentials and TLS (``rediss://``); the
    connection cap and arq's connect timeout ride beside it. Responses stay bytes, the
    shape arq reads.
    """
    settings = arq_settings()
    return {
        "url": settings.redis_url,
        "max_connections": settings.redis_max_connections,
        "decode_responses": False,
        "socket_connect_timeout": settings.redis_settings.conn_timeout,
        "env_prefix": settings.model_config.get("env_prefix") or "",
    }


@asynccontextmanager
async def arq_connection() -> AsyncIterator[Any]:
    """Lease the kit's pooled Redis client and yield an ``ArqRedis`` over its connection pool.

    Yielded as ``Any``: redis-py types each command's return as the union of its sync and
    async shapes, while this async client's commands always return an awaitable.
    """
    async with client_ctx(RedisClient, **enqueue_client_kwargs()) as client:
        connection_pool = client.connection_pool
        if not isinstance(connection_pool, ConnectionPool):
            raise TypeError(f"the kit Redis client's pool is {type(connection_pool).__name__}, not an asyncio pool")
        yield ArqRedis(
            pool_or_conn=connection_pool,
            job_serializer=job_serializer,
            job_deserializer=job_deserializer,
            default_queue_name=arq_settings().queue_name,
        )
