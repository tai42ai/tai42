import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.store.base import BaseStore
from langgraph.store.memory import InMemoryStore

logger = logging.getLogger(__name__)

CleanupFn = Callable[[], Awaitable[None]]
Resource = Any


async def create_store_resource(provider: str, conn_string: str | None = None, **kwargs) -> tuple[Resource, CleanupFn]:
    """
    Creates a long-lived connection resource for the Store.

    ARCHITECTURAL NOTES:
        Resources are cached indefinitely (no LRU) as this is a single-deployment instance.
    """

    store_kwargs = {k: v for k, v in kwargs.items() if v is not None}

    match provider:
        case "memory":
            store = InMemoryStore(**store_kwargs)

            async def close_memory():
                pass

            return store, close_memory

        case "sqlite":
            # WARNING: SQLITE CONCURRENCY
            # Uses a single shared connection. NOT for production/high-load.
            # Testing/Dev use only.

            if conn_string is None:
                raise ValueError("sqlite store provider requires a conn_string")

            import aiosqlite  # pyright: ignore[reportMissingImports]
            from langgraph.store.sqlite import AsyncSqliteStore  # pyright: ignore[reportMissingImports]

            conn = await aiosqlite.connect(conn_string)
            try:
                store = AsyncSqliteStore(conn, **store_kwargs)
                await store.setup()
            except BaseException:
                # setup failed: close the connection we just opened so it is
                # not leaked (no cleanup fn is returned on this path).
                await conn.close()
                raise

            async def close_sqlite():
                await conn.close()

            return store, close_sqlite

        case "postgres":
            if conn_string is None:
                raise ValueError("postgres store provider requires a conn_string")

            from langgraph.store.postgres import AsyncPostgresStore  # pyright: ignore[reportMissingImports]
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            pool = AsyncConnectionPool(
                conn_string,
                open=False,
                min_size=1,
                max_size=20,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            try:
                await pool.open()
                # Temp connection for setup
                async with pool.connection() as conn:
                    temp_store = AsyncPostgresStore(conn, **store_kwargs)
                    await temp_store.setup()
            except BaseException:
                # open/setup failed: close the pool so its connections are not
                # leaked (no cleanup fn is returned on this path).
                await pool.close()
                raise

            async def close_postgres():
                await pool.close()

            return pool, close_postgres

        case "redis":
            if conn_string is None:
                raise ValueError("redis store provider requires a conn_string")

            from langgraph.store.redis import AsyncRedisStore

            store = AsyncRedisStore(redis_url=conn_string, **store_kwargs)
            try:
                try:
                    await store.setup()
                except Exception as e:
                    # setup() is idempotent; only the benign "index already exists"
                    # race is safe to ignore — any other setup failure must surface.
                    if "index already exists" not in str(e).lower():
                        raise
                    logger.debug("Redis store setup already applied; ignoring: %s", e)
            except BaseException:
                # setup failed (or was cancelled): close the store we just opened
                # (it owns a redis pool + background task) so it is not leaked. No
                # cleanup fn is returned on this path.
                await store.__aexit__(None, None, None)
                raise

            async def close_redis():
                # The store owns the redis client (built from redis_url): its
                # __aexit__ cancels the batched-writer task and disconnects the
                # connection pool, matching the postgres/sqlite close paths.
                await store.__aexit__(None, None, None)

            return store, close_redis

        case _:
            raise ValueError(f"Unsupported store provider: {provider}")


def get_store_from_resource(provider: str, resource: Resource, **kwargs) -> BaseStore:
    match provider:
        case "memory" | "sqlite" | "redis":
            return resource
        case "postgres":
            # TTL: kwargs (including 'ttl') flow to the store, so rows are marked
            # with an expiration in Postgres. The TTL sweeper is not started here
            # (this is a transient object) — expired-row cleanup runs out of band
            # (e.g. pg_cron or a worker running `DELETE FROM store WHERE expire_at < NOW()`).
            from langgraph.store.postgres import AsyncPostgresStore  # pyright: ignore[reportMissingImports]

            return AsyncPostgresStore(resource, **kwargs)
        case _:
            raise ValueError(f"Unknown provider {provider}")
