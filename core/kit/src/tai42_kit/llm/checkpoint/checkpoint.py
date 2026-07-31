import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

logger = logging.getLogger(__name__)

CleanupFn = Callable[[], Awaitable[None]]
Resource = Any


async def create_checkpoint_resource(
    provider: str,
    conn_string: str | None = None,
) -> tuple[Resource, CleanupFn]:
    """
    Creates a long-lived connection resource for checkpoints.

    ARCHITECTURAL NOTES:
        This resource is intended to be cached indefinitely in the registry.
        Since this is a single-deployment instance, we do not use LRU eviction.
        The connection pool stays open for the lifecycle of the app.
    """
    match provider:
        case "memory":
            memory = InMemorySaver()

            async def close_memory():
                pass

            return memory, close_memory

        case "sqlite":
            # WARNING: SQLITE CONCURRENCY
            # This implementation shares a SINGLE connection across the entire application.
            # It is NOT production-ready for high concurrency.
            # Use this strictly for local development or testing.

            if conn_string is None:
                raise ValueError("sqlite checkpoint provider requires a conn_string")

            import aiosqlite  # pyright: ignore[reportMissingImports]
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # pyright: ignore[reportMissingImports]

            conn = await aiosqlite.connect(conn_string)
            try:
                temp_saver = AsyncSqliteSaver(conn)
                await temp_saver.setup()
            except BaseException:
                # setup failed: close the connection we just opened so it is
                # not leaked (no cleanup fn is returned on this path).
                await conn.close()
                raise

            async def close_sqlite():
                await conn.close()

            return conn, close_sqlite

        case "postgres":
            if conn_string is None:
                raise ValueError("postgres checkpoint provider requires a conn_string")

            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # pyright: ignore[reportMissingImports]
            from psycopg.rows import dict_row
            from psycopg_pool import AsyncConnectionPool

            # AsyncPostgresSaver needs connections that autocommit, skip prepared
            # statements, and yield dict rows; the pool applies these to every
            # connection it hands out (matching the store pool and langgraph's
            # documented AsyncPostgresSaver setup).
            pool = AsyncConnectionPool(
                conn_string,
                open=False,
                min_size=1,
                max_size=20,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
            try:
                await pool.open()
                async with pool.connection() as conn:
                    temp_saver = AsyncPostgresSaver(conn)
                    await temp_saver.setup()
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
                raise ValueError("redis checkpoint provider requires a conn_string")

            from langgraph.checkpoint.redis import AsyncRedisSaver

            from tai42_kit.llm.settings import llm_provider_settings

            # refresh_on_read makes the TTL measure idle time; None sets no TTL.
            ttl_minutes = llm_provider_settings().checkpoint_ttl_minutes
            ttl_config = {"default_ttl": ttl_minutes, "refresh_on_read": True} if ttl_minutes is not None else None

            saver = AsyncRedisSaver(redis_url=conn_string, ttl=ttl_config)
            try:
                try:
                    await saver.asetup()
                except Exception as e:
                    # asetup() is idempotent; only the benign "already exists" race
                    # is safe to ignore — any other setup failure must surface.
                    if "already exists" not in str(e).lower():
                        raise
                    logger.debug("Redis checkpoint setup already applied; ignoring: %s", e)
            except BaseException:
                # setup failed (or was cancelled): close the saver we just opened
                # (it owns a redis pool) so it is not leaked. No cleanup fn is
                # returned on this path.
                await saver.__aexit__(None, None, None)
                raise

            async def close_redis():
                # The saver owns the redis client (built from redis_url): its
                # __aexit__ disconnects the connection pool, matching the
                # postgres/sqlite close paths.
                await saver.__aexit__(None, None, None)

            return saver, close_redis

        case _:
            raise ValueError(f"Unsupported checkpoint provider: {provider}")


def get_saver_from_resource(provider: str, resource: Resource) -> BaseCheckpointSaver:
    match provider:
        case "memory":
            return resource
        case "sqlite":
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # pyright: ignore[reportMissingImports]

            return AsyncSqliteSaver(resource)
        case "postgres":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # pyright: ignore[reportMissingImports]

            return AsyncPostgresSaver(resource)
        case "redis":
            return resource
        case _:
            raise ValueError(f"Unknown provider {provider}")
