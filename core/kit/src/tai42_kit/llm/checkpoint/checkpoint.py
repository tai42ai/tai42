"""Checkpoint resource and saver creation: one resource per store, a codec and a guard around every saver."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import ormsgpack
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from tai42_kit.clients.settings import PostgresConnectionSettings, RedisConnectionSettings
from tai42_kit.llm.checkpoint.ledger import (
    FinishedThreadLedger,
    MemoryFinishedThreadLedger,
    PostgresFinishedThreadLedger,
    RedisFinishedThreadLedger,
    SqliteFinishedThreadLedger,
)
from tai42_kit.llm.checkpoint.providers import CheckpointProviderFacts, checkpoint_provider_facts
from tai42_kit.llm.checkpoint.store_format import ensure_store_format
from tai42_kit.llm.settings import llm_provider_settings
from tai42_kit.settings import not_configured_message
from tai42_kit.utils.data.json_schema_util import MSGPACK_INT_MAX, MSGPACK_INT_MIN, find_oversized_int

if TYPE_CHECKING:
    from psycopg import AsyncConnection
    from psycopg.rows import DictRow
    from redis.asyncio import Redis as AsyncRedis

    from tai42_kit.llm.checkpoint.codec import CheckpointCodec

logger = logging.getLogger(__name__)

CleanupFn = Callable[[], Awaitable[None]]


class CheckpointSerializationError(Exception):
    """A checkpoint value could not be stored faithfully, or a stored value could not be read back.

    The guard converts ormsgpack's ``MsgpackEncodeError`` into this typed error so a value
    the serializer cannot encode fails loudly — never a silent pickle fallback, never an
    opaque engine crash. The checkpoint guard owns the storage integer range: an integer
    outside the native msgpack range ``[-2**63, 2**64-1]`` has no encoding, and the message
    NAMES the offending path. The Redis codec raises it for a value it cannot store
    faithfully and for a stored envelope it cannot decode.
    """


class CheckpointDeleteError(Exception):
    """A checkpoint thread still holds stored documents after a delete round that removed none of them."""


def _serialization_error(obj: Any, cause: ormsgpack.MsgpackEncodeError) -> CheckpointSerializationError:
    # The reactive guard fires only after msgpack aborts, so it names the true
    # encode-failure culprit: an integer outside the native msgpack range. A value
    # in (INT64_MAX, MSGPACK_INT_MAX] encoded fine and is not the culprit.
    overflow = find_oversized_int(obj, minimum=MSGPACK_INT_MIN, maximum=MSGPACK_INT_MAX)
    if overflow is not None:
        path, value = overflow
        return CheckpointSerializationError(
            f"checkpoint value at {path} = {value} exceeds the native msgpack integer range "
            f"[{MSGPACK_INT_MIN}, {MSGPACK_INT_MAX}] and has no msgpack integer encoding"
        )
    return CheckpointSerializationError(f"checkpoint value could not be msgpack-encoded: {cause}")


class _GuardedSerializer:
    """Wrap a saver's serializer: a codec (when the provider has one) and the encode-failure guard.

    A ``MsgpackEncodeError`` becomes a :class:`CheckpointSerializationError` naming the offending path.
    ``dumps_metadata_typed`` encodes checkpoint metadata through the inner serializer alone (no codec).
    All other serializer methods delegate unchanged.
    """

    def __init__(self, inner: Any, codec: CheckpointCodec | None = None) -> None:
        self._inner = inner
        self._codec = codec

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        try:
            if self._codec is not None:
                return self._codec.dumps_typed(self._inner, obj)
            return self._inner.dumps_typed(obj)
        except ormsgpack.MsgpackEncodeError as exc:
            raise _serialization_error(obj, exc) from exc

    def dumps_metadata_typed(self, obj: Any) -> tuple[str, bytes]:
        try:
            return self._inner.dumps_typed(obj)
        except ormsgpack.MsgpackEncodeError as exc:
            raise _serialization_error(obj, exc) from exc

    def dumps(self, obj: Any) -> bytes:
        try:
            return self._inner.dumps(obj)
        except ormsgpack.MsgpackEncodeError as exc:
            raise _serialization_error(obj, exc) from exc

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        if self._codec is not None:
            return self._codec.loads_typed(self._inner, data)
        return self._inner.loads_typed(data)

    def loads(self, data: bytes) -> Any:
        return self._inner.loads(data)

    def _revive_if_needed(self, obj: Any) -> Any:
        return self._inner._revive_if_needed(obj)


def _guard_saver_serialization(saver: BaseCheckpointSaver, facts: CheckpointProviderFacts) -> BaseCheckpointSaver:
    """Install the provider's codec and the serialization guard on ``saver.serde`` (idempotent).

    Every saver the registry hands to a graph flows through here, so no checkpoint write can
    reach the serializer unguarded. Redis gets the faithful JSON codec (with the inline compressed
    envelope); Postgres compresses large blobs; sqlite and memory get the guard alone.
    """
    if isinstance(saver.serde, _GuardedSerializer):
        return saver
    threshold = llm_provider_settings().checkpoint_inline_compress_bytes
    if facts.faithful_codec:
        from tai42_kit.llm.checkpoint.codec import FaithfulRedisSerializer, RedisFaithfulCodec

        saver.serde = _GuardedSerializer(FaithfulRedisSerializer(), RedisFaithfulCodec(threshold))
    elif facts.inline_compress:
        from tai42_kit.llm.checkpoint.codec import BlobCompressCodec

        saver.serde = _GuardedSerializer(saver.serde, BlobCompressCodec(threshold))
    else:
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


@dataclass(frozen=True)
class CheckpointResource:
    """The long-lived resource behind one ``provider::conn_string`` checkpoint store.

    ``handle`` is what the provider's saver is built on: the ``InMemorySaver`` (memory), the
    aiosqlite connection (sqlite), the psycopg pool (postgres) or the saver itself (redis).
    ``ledger`` is the store's finished-thread ledger, kept in the same store as the checkpoints.
    ``redis_client`` is the client the kit built and injected (redis only).
    """

    provider: str
    handle: Any
    ledger: FinishedThreadLedger
    redis_client: AsyncRedis | None = None


async def _create_memory_checkpoint(conn_string: str | None) -> tuple[CheckpointResource, CleanupFn]:
    memory = InMemorySaver()

    async def close_memory():
        pass

    return CheckpointResource("memory", memory, MemoryFinishedThreadLedger()), close_memory


async def _create_sqlite_checkpoint(conn_string: str | None) -> tuple[CheckpointResource, CleanupFn]:
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
        ledger = SqliteFinishedThreadLedger(conn)
        await ledger.setup()
    except BaseException:
        # setup failed: close the connection we just opened so it is
        # not leaked (no cleanup fn is returned on this path).
        await conn.close()
        raise

    async def close_sqlite():
        await conn.close()

    return CheckpointResource("sqlite", conn, ledger), close_sqlite


async def _create_postgres_checkpoint(conn_string: str | None) -> tuple[CheckpointResource, CleanupFn]:
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # pyright: ignore[reportMissingImports]

    from tai42_kit.clients.impl.postgres import open_dedicated_pool
    from tai42_kit.llm._langgraph_postgres import LANGGRAPH_CONNECTION_KWARGS

    # An unset conn string means the base Postgres namespace: its DSN builder raises a named
    # error if that identity is also unset, and its deployment sizes size this pool.
    target: PostgresConnectionSettings | str = PostgresConnectionSettings() if conn_string is None else conn_string
    pool = await open_dedicated_pool(target, role="checkpoint", connection_kwargs=dict(LANGGRAPH_CONNECTION_KWARGS))
    try:
        # The pool's per-connection kwargs set row_factory=dict_row, so every checkout
        # yields dict rows; the shared pool type is row-agnostic, so narrow it to the
        # dict-row connection the saver needs.
        async with pool.connection() as conn:
            temp_saver = AsyncPostgresSaver(cast("AsyncConnection[DictRow]", conn))
            await temp_saver.setup()
        ledger = PostgresFinishedThreadLedger(pool)
        await ledger.setup()
    except BaseException:
        # setup failed: close the pool so its connections are not
        # leaked (no cleanup fn is returned on this path).
        await pool.close()
        raise

    async def close_postgres():
        await pool.close()

    return CheckpointResource("postgres", pool, ledger), close_postgres


async def _create_redis_checkpoint(conn_string: str | None) -> tuple[CheckpointResource, CleanupFn]:
    if conn_string is None:
        # An unset conn string means the base Redis namespace.
        conn_string = RedisConnectionSettings().redis_url
    if conn_string is None:
        raise ValueError(REDIS_CHECKPOINT_NOT_CONFIGURED_MESSAGE)

    from redis.asyncio import Redis as AsyncRedis

    from tai42_kit.llm.checkpoint.codec import GuardedAsyncRedisSaver

    # Every key a write stamps carries the waiting retention, and a read never re-arms it: a
    # thread lives that long after its last write. A parked run's last write is its park
    # checkpoint, and the latest checkpoint carries its channel values inline, so older
    # checkpoints expiring first never breaks a resume.
    ttl_config = {"default_ttl": llm_provider_settings().checkpoint_retention_waiting_minutes, "refresh_on_read": False}

    # The client is injected, never left to the saver: handed a URL, the
    # saver builds its own client, marks itself the owner, and its teardown
    # then clears the redisvl search indexes' client reference — after which
    # those indexes re-resolve a connection from the BARE ``REDIS_URL``
    # environment variable, blind to the URL resolved above (which may have
    # come from TAI_DEFAULT_REDIS_URL or the conn-string setting). Injecting
    # the client keeps the saver a non-owner, so that env fallback is
    # unreachable. ``connection_args`` is deliberately not passed: the saver
    # consults it only while building a client of its own.
    client = AsyncRedis.from_url(conn_string)
    saver = GuardedAsyncRedisSaver(redis_client=client, ttl=ttl_config)
    _guard_saver_serialization(saver, checkpoint_provider_facts("redis"))

    async def close_redis():
        # Ownership is explicit and one-way: the kit built the client, so the
        # kit closes it. The saver's teardown runs first — for a non-owner
        # that call is inert (the whole body is ownership-gated), kept so a
        # version that does release state still gets it, and so there is no
        # double-close — and the client closes even if that teardown raises.
        try:
            await saver.__aexit__(None, None, None)
        finally:
            await client.aclose()

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
        # setup failed (or was cancelled): run the same cleanup path so the
        # saver and the client are not leaked. No cleanup fn is returned here.
        await close_redis()
        raise

    return CheckpointResource("redis", saver, RedisFinishedThreadLedger(client), client), close_redis


_BUILDERS: dict[str, Callable[[str | None], Awaitable[tuple[CheckpointResource, CleanupFn]]]] = {
    "memory": _create_memory_checkpoint,
    "sqlite": _create_sqlite_checkpoint,
    "postgres": _create_postgres_checkpoint,
    "redis": _create_redis_checkpoint,
}


async def create_checkpoint_resource(
    provider: str,
    conn_string: str | None = None,
) -> tuple[CheckpointResource, CleanupFn]:
    """Creates a long-lived connection resource for checkpoints, its ledger, and checks its store format.

    A ``None`` conn string falls back per provider to the base connection
    namespace: ``redis`` to the base Redis URL (``REDIS_URL`` /
    ``TAI_DEFAULT_REDIS_URL``), ``postgres`` to the base Postgres DSN (``PG_*``).
    ``sqlite`` requires an explicit path; ``memory`` needs none. A store holding
    data written in an older format raises :class:`CheckpointStoreFormatError`.

    ARCHITECTURAL NOTES:
        This resource is intended to be cached indefinitely in the registry.
        Since this is a single-deployment instance, we do not use LRU eviction.
        The connection pool stays open for the lifecycle of the app.
    """
    checkpoint_provider_facts(provider)
    resource, cleanup = await _BUILDERS[provider](conn_string)
    try:
        await ensure_store_format(resource)
    except BaseException:
        await cleanup()
        raise
    return resource, cleanup


def get_saver_from_resource(provider: str, resource: CheckpointResource) -> BaseCheckpointSaver:
    """Build the checkpoint saver for ``provider`` from an already-open ``resource``, codec-installed and guarded."""
    facts = checkpoint_provider_facts(provider)
    if resource.provider != provider:
        raise ValueError(f"checkpoint resource of provider {resource.provider!r} cannot serve provider {provider!r}")
    # Every branch returns through the serialization guard: this is the single
    # choke point that yields the saver a graph checkpoints through, so no saver
    # can serialize a checkpoint value unguarded.
    match provider:
        case "sqlite":
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # pyright: ignore[reportMissingImports]

            saver: BaseCheckpointSaver = AsyncSqliteSaver(resource.handle)
        case "postgres":
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver  # pyright: ignore[reportMissingImports]

            saver = AsyncPostgresSaver(resource.handle)
        case _:
            saver = resource.handle
    return _guard_saver_serialization(saver, facts)
