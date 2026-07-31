"""Process-wide cached :class:`~arq.connections.ArqRedis` pool.

arq-native operations (enqueue, ``Job`` status/result, schedule hashes) share
one lazily created pool configured with this backend's JSON job (de)serializers.
``close`` is wired to the app shutdown hook (see
:mod:`tai42_backend_arq.lifecycle`).
"""

from __future__ import annotations

import asyncio

from arq import ArqRedis, create_pool

from tai42_backend_arq.settings import arq_settings, job_deserializer, job_serializer


class RedisPoolManager:
    _pool: ArqRedis | None = None
    # Lazily created on the running loop it is first awaited on; dropped with the pool.
    _lock: asyncio.Lock | None = None

    @classmethod
    async def get(cls) -> ArqRedis:
        if cls._pool is None:
            # Double-checked under the lock so two pools can't race into existence.
            if cls._lock is None:
                cls._lock = asyncio.Lock()
            async with cls._lock:
                if cls._pool is None:
                    cls._pool = await create_pool(
                        arq_settings().redis_settings,
                        job_serializer=job_serializer,
                        job_deserializer=job_deserializer,
                    )
        return cls._pool

    @classmethod
    async def close(cls) -> None:
        if cls._pool:
            await cls._pool.aclose()
            cls._pool = None
        cls._lock = None
