"""App lifecycle hooks: close the shared ArqRedis pool at shutdown."""

from __future__ import annotations

from tai42_contract.app import tai42_app

from tai42_backend_arq.pool import RedisPoolManager


@tai42_app.lifecycle.on_shutdown
async def close_arq_pool() -> None:
    await RedisPoolManager.close()
