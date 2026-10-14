"""The per-thread value stores a checkpoint saver keeps its shared values in, beside its own keys or tables.

A value lives as long as its thread: the store never deletes one while the thread lives (any older
checkpoint of the thread may reference it) and deletes them all with the thread. On Redis each value
is a key carrying the saver's waiting retention as its TTL, re-armed by every write that references
it, so a value expires no earlier than the last checkpoint referencing it; a per-thread index set lists
the thread's digests for the delete. On Postgres the values are rows of the kit table
``tai42_checkpoint_values`` deleted with the thread.
"""

from __future__ import annotations

from collections.abc import Awaitable, Collection, Mapping
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from langgraph.checkpoint.redis.util import to_storage_safe_id

from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError
from tai42_kit.llm.checkpoint.chunking import StoredBlob

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool
    from redis.asyncio import Redis as AsyncRedis
    from redis.asyncio.cluster import RedisCluster as AsyncRedisCluster

REDIS_VALUE_KEY_PREFIX: Final = "tai42:checkpoint:value:"
REDIS_VALUE_INDEX_PREFIX: Final = "tai42:checkpoint:values:"

# Digests deleted per DEL command when a thread is deleted.
_DELETE_BATCH: Final = 500


class CheckpointValueStore(Protocol):
    """Where one saver keeps its threads' shared values."""

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        """Re-arm the retention of the held ``digests`` of ``thread_id``; return those it does not hold."""
        ...

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        """Store ``blobs`` (by digest) for ``thread_id``."""
        ...

    async def get(self, thread_id: str, digests: Collection[str]) -> dict[str, StoredBlob]:
        """The held blobs among ``digests`` of ``thread_id``; a digest it does not hold is absent."""
        ...

    async def delete_thread(self, thread_id: str) -> None:
        """Delete every value ``thread_id`` holds."""
        ...


def _redis_tag(thread_id: str) -> str:
    # The braces are a cluster hash tag: a thread's values and its index share one slot.
    return "{" + to_storage_safe_id(thread_id) + "}"


def _redis_record(blob: StoredBlob) -> bytes:
    return blob.type.encode() + b"\n" + blob.data


def _redis_blob(raw: Any, key: str) -> StoredBlob:
    if not isinstance(raw, bytes):
        raise CheckpointSerializationError(f"checkpoint value key {key} holds {type(raw).__name__}, not bytes")
    type_, separator, data = raw.partition(b"\n")
    if not separator:
        raise CheckpointSerializationError(f"checkpoint value key {key} holds no type tag")
    return StoredBlob(type_.decode(), data)


class RedisCheckpointValueStore:
    """Values as Redis keys with the saver's waiting retention, and a per-thread index set of digests."""

    def __init__(self, client: AsyncRedis | AsyncRedisCluster, ttl_seconds: int | None) -> None:
        """Store through ``client``; every key a write stamps carries ``ttl_seconds`` (``None``: no expiry)."""
        self._client = client
        self._ttl = ttl_seconds

    @staticmethod
    def value_key(thread_id: str, digest: str) -> str:
        """The key holding ``digest`` of ``thread_id``."""
        return f"{REDIS_VALUE_KEY_PREFIX}{_redis_tag(thread_id)}:{digest}"

    @staticmethod
    def index_key(thread_id: str) -> str:
        """The set listing the digests ``thread_id`` holds."""
        return f"{REDIS_VALUE_INDEX_PREFIX}{_redis_tag(thread_id)}"

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        """Re-arm each held digest's TTL and the index's; one round trip."""
        ordered = list(digests)
        pipeline = self._client.pipeline(transaction=False)
        for digest in ordered:
            key = self.value_key(thread_id, digest)
            if self._ttl is None:
                pipeline.exists(key)
            else:
                pipeline.expire(key, self._ttl)
        if self._ttl is not None:
            pipeline.expire(self.index_key(thread_id), self._ttl)
        results = await pipeline.execute()
        return {digest for digest, held in zip(ordered, results, strict=False) if not held}

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        """Store every blob with the TTL and add its digest to the index; one round trip."""
        if not blobs:
            return
        index = self.index_key(thread_id)
        pipeline = self._client.pipeline(transaction=False)
        for digest, blob in blobs.items():
            pipeline.set(self.value_key(thread_id, digest), _redis_record(blob), ex=self._ttl)
        pipeline.sadd(index, *blobs)
        if self._ttl is not None:
            pipeline.expire(index, self._ttl)
        await pipeline.execute()

    async def get(self, thread_id: str, digests: Collection[str]) -> dict[str, StoredBlob]:
        """The held blobs among ``digests``; one ``MGET``."""
        ordered = list(digests)
        if not ordered:
            return {}
        keys = [self.value_key(thread_id, digest) for digest in ordered]
        raws = await self._client.mget(keys)
        return {
            digest: _redis_blob(raw, key)
            for digest, key, raw in zip(ordered, keys, raws, strict=True)
            if raw is not None
        }

    async def delete_thread(self, thread_id: str) -> None:
        """Delete every value the index lists, then the index."""
        index = self.index_key(thread_id)
        members = await cast("Awaitable[set[Any]]", self._client.smembers(index))
        digests = sorted(member.decode() if isinstance(member, bytes) else str(member) for member in members)
        for start in range(0, len(digests), _DELETE_BATCH):
            batch = digests[start : start + _DELETE_BATCH]
            await self._client.delete(*(self.value_key(thread_id, digest) for digest in batch))
        await self._client.delete(index)


class PostgresCheckpointValueStore:
    """Values as rows of the kit's value table, deleted with their thread."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """Store through ``pool``."""
        self._pool = pool

    async def setup(self) -> None:
        """Create the value table beside the saver's tables."""
        from tai42_kit.llm.checkpoint import postgres_store

        await postgres_store.create_value_table(self._pool)

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        """The digests the table does not hold for ``thread_id`` (a row has no retention to re-arm)."""
        from tai42_kit.llm.checkpoint import postgres_store

        wanted = set(digests)
        held = await postgres_store.held_values(self._pool, thread_id, wanted)
        return wanted - held

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        """Insert every blob; a digest already held is kept as it is."""
        from tai42_kit.llm.checkpoint import postgres_store

        if blobs:
            await postgres_store.insert_values(
                self._pool, thread_id, [(digest, blob.type, blob.data) for digest, blob in blobs.items()]
            )

    async def get(self, thread_id: str, digests: Collection[str]) -> dict[str, StoredBlob]:
        """The held blobs among ``digests``; one statement."""
        from tai42_kit.llm.checkpoint import postgres_store

        rows = await postgres_store.read_values(self._pool, thread_id, set(digests))
        return {digest: StoredBlob(type_, data) for digest, type_, data in rows}

    async def delete_thread(self, thread_id: str) -> None:
        """Delete every row of ``thread_id``."""
        from tai42_kit.llm.checkpoint import postgres_store

        await postgres_store.delete_values(self._pool, thread_id)
