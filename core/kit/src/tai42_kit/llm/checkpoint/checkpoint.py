import logging
from collections.abc import Awaitable, Callable
from typing import Any

import ormsgpack
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from tai42_kit.clients.settings import PostgresConnectionSettings, RedisConnectionSettings
from tai42_kit.settings import not_configured_message
from tai42_kit.utils.data.json_schema_util import MSGPACK_INT_MAX, MSGPACK_INT_MIN, find_oversized_int

logger = logging.getLogger(__name__)

CleanupFn = Callable[[], Awaitable[None]]
Resource = Any


class CheckpointSerializationError(Exception):
    """A checkpoint value could not be msgpack-encoded by the saver's serializer.

    The universal guard converts ormsgpack's ``MsgpackEncodeError`` into this
    typed error so a value the serializer cannot encode fails loudly — never a
    silent pickle fallback, never an opaque engine crash. When the cause is an
    integer outside the native msgpack integer range (the door schema-level int64
    injection cannot reach — plain tool-call args and tool results routed into
    flow state), the message NAMES the offending path.
    """


def _raise_serialization_error(obj: Any, cause: ormsgpack.MsgpackEncodeError) -> None:
    # The reactive guard fires only after msgpack aborts, so it names the true
    # encode-failure culprit: an integer outside the native msgpack range. A value
    # in (INT64_MAX, MSGPACK_INT_MAX] encoded fine and is not the culprit.
    overflow = find_oversized_int(obj, minimum=MSGPACK_INT_MIN, maximum=MSGPACK_INT_MAX)
    if overflow is not None:
        path, value = overflow
        raise CheckpointSerializationError(
            f"checkpoint value at {path} = {value} exceeds the native msgpack integer range "
            f"[{MSGPACK_INT_MIN}, {MSGPACK_INT_MAX}] and has no msgpack integer encoding"
        ) from cause
    raise CheckpointSerializationError(f"checkpoint value could not be msgpack-encoded: {cause}") from cause


class _GuardedSerializer:
    """Wraps a saver's serializer so a ``MsgpackEncodeError`` becomes a
    :class:`CheckpointSerializationError` naming the offending path. All other
    serializer methods delegate unchanged."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        try:
            return self._inner.dumps_typed(obj)
        except ormsgpack.MsgpackEncodeError as exc:
            _raise_serialization_error(obj, exc)
            raise  # unreachable; _raise_serialization_error always raises

    def dumps(self, obj: Any) -> bytes:
        try:
            return self._inner.dumps(obj)
        except ormsgpack.MsgpackEncodeError as exc:
            _raise_serialization_error(obj, exc)
            raise  # unreachable; _raise_serialization_error always raises

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        return self._inner.loads_typed(data)

    def loads(self, data: bytes) -> Any:
        return self._inner.loads(data)


def _guard_saver_serialization(saver: BaseCheckpointSaver) -> BaseCheckpointSaver:
    """Wrap ``saver.serde`` in the serialization guard (idempotent). Every saver
    the registry hands to a graph flows through here, so no checkpoint write can
    reach the serializer unguarded."""
    if not isinstance(saver.serde, _GuardedSerializer):
        saver.serde = _GuardedSerializer(saver.serde)
    return saver

# Resolution order: the conn string first, then the base Redis namespace. The
# offload needs a module-capable Redis, so the message also names the module
# requirement a plain Redis can't meet. Shared verbatim by the boot gate.
REDIS_CHECKPOINT_NOT_CONFIGURED_MESSAGE = (
    not_configured_message(
        "the Redis checkpoint",
        "LLM_PROVIDER_CHECKPOINT_CONN_STRING",
        "the base Redis URL REDIS_URL / TAI_DEFAULT_REDIS_URL",
    )
    + " The target Redis must provide the JSON and search modules (RedisJSON +"
    + " RediSearch); a plain Redis fails mid-run on FT.* commands."
)


async def create_checkpoint_resource(
    provider: str,
    conn_string: str | None = None,
) -> tuple[Resource, CleanupFn]:
    """
    Creates a long-lived connection resource for checkpoints.

    A ``None`` conn string falls back per provider to the base connection
    namespace: ``redis`` to the base Redis URL (``REDIS_URL`` /
    ``TAI_DEFAULT_REDIS_URL``), ``postgres`` to the base Postgres DSN (``PG_*``).
    ``sqlite`` requires an explicit path; ``memory`` needs none.

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
                # An unset conn string means the base Postgres namespace; the DSN
                # builder raises a named error if that identity is also unset.
                conn_string = PostgresConnectionSettings().pg_dsn

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
                # An unset conn string means the base Redis namespace.
                conn_string = RedisConnectionSettings().redis_url
            if conn_string is None:
                raise ValueError(REDIS_CHECKPOINT_NOT_CONFIGURED_MESSAGE)

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
    # Every branch returns through the serialization guard: this is the single
    # choke point that yields the saver a graph checkpoints through, so no saver
    # can serialize a checkpoint value unguarded.
    match provider:
        case "memory":
            return _guard_saver_serialization(resource)
        case "sqlite":
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # pyright: ignore[reportMissingImports]

            return _guard_saver_serialization(AsyncSqliteSaver(resource))
        case "postgres":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # pyright: ignore[reportMissingImports]

            return _guard_saver_serialization(AsyncPostgresSaver(resource))
        case "redis":
            return _guard_saver_serialization(resource)
        case _:
            raise ValueError(f"Unknown provider {provider}")
