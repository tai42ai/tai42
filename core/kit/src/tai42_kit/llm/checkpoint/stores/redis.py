"""The ``redis`` checkpoint store: the kit's Redis saver over a client the kit builds, threads kept by key TTL.

The saver stores each checkpoint as one JSON document with its channel values inline, so a store that
copies every channel into every checkpoint. Every key a write stamps carries the thread's waiting
retention as its TTL; a thread whose owner declared another waiting is written through a saver with
that TTL. The finished-thread ledger is a sorted set on the same client, the shared values are keys
with the same TTL, and the store-format marker is one key.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from tai42_kit.llm.checkpoint.ledger import as_utc
from tai42_kit.llm.checkpoint.providers import (
    CheckpointProvider,
    CheckpointProviderFacts,
    CheckpointWrite,
    register_checkpoint_provider,
)
from tai42_kit.llm.checkpoint.store_format import generation_marker
from tai42_kit.llm.checkpoint.value_store import StoredBlob
from tai42_kit.llm.settings import llm_provider_settings
from tai42_kit.settings import not_configured_message

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence
    from datetime import datetime

    from langgraph.checkpoint.base import BaseCheckpointSaver, ChannelVersions, Checkpoint
    from redis.asyncio import Redis as AsyncRedis

    from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource
    from tai42_kit.llm.checkpoint.codec import GuardedAsyncRedisSaver
    from tai42_kit.llm.checkpoint.envelope import CheckpointCodec
    from tai42_kit.llm.checkpoint.redis_values import RedisJsonValueEncoding
    from tai42_kit.llm.checkpoint.retention import ThreadRetention
    from tai42_kit.llm.checkpoint.value_store import CheckpointValueStore

logger = logging.getLogger(__name__)

# Resolution order: the conn string first, then the base Redis namespace. The saver needs a
# module-capable Redis, so the message also names the module requirement a plain Redis can't meet.
REDIS_CHECKPOINT_NOT_CONFIGURED_MESSAGE: Final = (
    not_configured_message(
        "the Redis checkpoint",
        "LLM_PROVIDER_CHECKPOINT_CONN_STRING",
        "the base Redis URL REDIS_URL / TAI_DEFAULT_REDIS_URL",
    )
    + " The target Redis must provide the JSON and search modules (RedisJSON +"
    + " RediSearch); a plain Redis fails mid-run on FT.* commands."
)

# The key holding the store's format generation.
REDIS_FORMAT_KEY: Final = "tai42:checkpoint:format"

# The sorted set of finished threads (member = thread id, score = epoch seconds of the due time).
REDIS_FINISHED_THREADS_KEY: Final = "tai42:checkpoint:finished"

REDIS_VALUE_KEY_PREFIX: Final = "tai42:checkpoint:value:"
REDIS_VALUE_INDEX_PREFIX: Final = "tai42:checkpoint:values:"

# Digests deleted per DEL command when a thread is deleted.
_DELETE_BATCH: Final = 500


@dataclass(frozen=True)
class RedisHandle:
    """What the Redis store's savers are built on: the store's own saver and the client the kit built."""

    saver: GuardedAsyncRedisSaver
    client: AsyncRedis


class RedisFinishedThreadLedger:
    """The ledger of a Redis checkpoint store: a sorted set on the kit's own client.

    A thread's waiting retention is the TTL its saver stamps on every key it writes, so ``start``
    records no declaration and ``declared_waiting`` knows none. Each mark re-arms the set's expiry to
    the platform's waiting retention, the longest any thread lives: the threads it names expire on
    their own key TTL within that horizon, so with no sweep ever scheduled the set outlives its
    newest mark by at most that horizon.
    """

    def __init__(self, client: AsyncRedis) -> None:
        """``client``: the Redis client the kit built for the checkpoint saver."""
        self._client = client

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        """Remove the finished marks of ``thread_ids``."""
        await self.forget(thread_ids)

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        """Record ``thread_ids`` as finished, due at ``due_at``."""
        score = as_utc(due_at).timestamp()
        if not thread_ids:
            # ZADD takes at least one member.
            return
        ttl_seconds = llm_provider_settings().checkpoint_retention_waiting_minutes * 60
        pipeline = self._client.pipeline(transaction=True)
        pipeline.zadd(REDIS_FINISHED_THREADS_KEY, dict.fromkeys(thread_ids, score))
        pipeline.expire(REDIS_FINISHED_THREADS_KEY, ttl_seconds)
        await pipeline.execute()

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids due at or before ``cutoff``, earliest first."""
        members = await self._client.zrangebyscore(
            REDIS_FINISHED_THREADS_KEY, "-inf", as_utc(cutoff).timestamp(), start=0, num=limit
        )
        return [member.decode() if isinstance(member, bytes) else member for member in members]

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        if not thread_ids:
            # ZREM takes at least one member.
            return
        await self._client.zrem(REDIS_FINISHED_THREADS_KEY, *thread_ids)

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        """Always empty: the waiting retention lives on the thread's own keys."""
        return {}

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """Always empty: every key a write stamps carries the thread's waiting retention as its TTL.

        So no thread past its waiting horizon remains in this store.
        """
        as_utc(now)
        return []


def _record(blob: StoredBlob) -> bytes:
    return blob.type.encode() + b"\n" + blob.data


def _blob(raw: Any, key: str) -> StoredBlob:
    from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError

    if not isinstance(raw, bytes):
        raise CheckpointSerializationError(f"checkpoint value key {key} holds {type(raw).__name__}, not bytes")
    type_, separator, data = raw.partition(b"\n")
    if not separator:
        raise CheckpointSerializationError(f"checkpoint value key {key} holds no type tag")
    return StoredBlob(type_.decode(), data)


class RedisCheckpointValueStore:
    """Values as Redis keys with a waiting retention as their TTL, and a per-thread index set of digests."""

    def __init__(self, client: AsyncRedis, ttl_seconds: int | None) -> None:
        """Store through ``client``; every key a write stamps carries ``ttl_seconds`` (``None``: no expiry)."""
        from langgraph.checkpoint.redis.util import to_storage_safe_id

        self._client = client
        self._ttl = ttl_seconds
        self._safe_id = to_storage_safe_id

    def _tag(self, thread_id: str) -> str:
        # The braces are a cluster hash tag: a thread's values and its index share one slot.
        return "{" + self._safe_id(thread_id) + "}"

    def value_key(self, thread_id: str, digest: str) -> str:
        """The key holding ``digest`` of ``thread_id``."""
        return f"{REDIS_VALUE_KEY_PREFIX}{self._tag(thread_id)}:{digest}"

    def index_key(self, thread_id: str) -> str:
        """The set listing the digests ``thread_id`` holds."""
        return f"{REDIS_VALUE_INDEX_PREFIX}{self._tag(thread_id)}"

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
            pipeline.set(self.value_key(thread_id, digest), _record(blob), ex=self._ttl)
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
            digest: _blob(raw, key) for digest, key, raw in zip(ordered, keys, raws, strict=True) if raw is not None
        }

    async def held_digests(self, thread_id: str) -> set[str]:
        """Every digest the thread's index lists."""
        members = await cast("Awaitable[set[Any]]", self._client.smembers(self.index_key(thread_id)))
        return {member.decode() if isinstance(member, bytes) else str(member) for member in members}

    async def delete_thread(self, thread_id: str) -> None:
        """Delete every value the index lists, then the index."""
        index = self.index_key(thread_id)
        members = await cast("Awaitable[set[Any]]", self._client.smembers(index))
        digests = sorted(member.decode() if isinstance(member, bytes) else str(member) for member in members)
        for start in range(0, len(digests), _DELETE_BATCH):
            batch = digests[start : start + _DELETE_BATCH]
            await self._client.delete(*(self.value_key(thread_id, digest) for digest in batch))
        await self._client.delete(index)


def _ttl_config(waiting_minutes: int) -> dict[str, Any]:
    # Every key a write stamps carries the waiting retention, and a read never re-arms it: a
    # thread lives that long after its last write. A parked run's last write is its park
    # checkpoint, which carries its channel values inline or references shared values that
    # same write re-armed, so older checkpoints expiring first never breaks a resume.
    return {"default_ttl": waiting_minutes, "refresh_on_read": False}


async def _setup(saver: Any) -> None:
    try:
        await saver.asetup()
    except Exception as e:
        # asetup() is idempotent; only the benign "already exists" race
        # is safe to ignore — any other setup failure must surface.
        if "already exists" not in str(e).lower():
            raise
        logger.debug("Redis checkpoint setup already applied; ignoring: %s", e)


@register_checkpoint_provider
class RedisCheckpointProvider(CheckpointProvider):
    """The kit's Redis saver (LangGraph's ``AsyncRedisSaver``) over a client the kit builds and injects."""

    name: ClassVar[str] = "redis"
    facts: ClassVar[CheckpointProviderFacts] = CheckpointProviderFacts(
        durable=True, retention="native_ttl", channel_storage="per_checkpoint"
    )

    async def open(self, conn_string: str | None) -> tuple[CheckpointResource, Callable[[], Awaitable[None]]]:
        """Build the client, the saver stamping the platform's waiting retention, and set up its indexes.

        ``None`` opens the base Redis namespace; with none configured it raises
        :data:`REDIS_CHECKPOINT_NOT_CONFIGURED_MESSAGE`.
        """
        from tai42_kit.clients.settings import RedisConnectionSettings

        if conn_string is None:
            conn_string = RedisConnectionSettings().redis_url
        if conn_string is None:
            raise ValueError(REDIS_CHECKPOINT_NOT_CONFIGURED_MESSAGE)

        from tai42_kit.clients.impl.redis import async_redis_from_url
        from tai42_kit.llm.checkpoint import codec
        from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource

        waiting = llm_provider_settings().checkpoint_retention_waiting_minutes
        # The client is injected, never left to the saver: handed a URL, the saver builds its own
        # client, marks itself the owner, and its teardown then clears the redisvl search indexes'
        # client reference — after which those indexes re-resolve a connection from the BARE
        # ``REDIS_URL`` environment variable, blind to the URL resolved above. Injecting the client
        # keeps the saver a non-owner, so that fallback is unreachable. ``connection_args`` is not
        # passed: the saver consults it only while building a client of its own.
        client = async_redis_from_url(conn_string)
        saver = codec.GuardedAsyncRedisSaver(redis_client=client, ttl=_ttl_config(waiting))

        async def close() -> None:
            # The kit built the client, so the kit closes it. The saver's teardown runs first — for a
            # non-owner it releases nothing — and the client closes even if that teardown raises.
            try:
                await saver.__aexit__(None, None, None)
            finally:
                await client.aclose()

        try:
            await _setup(saver)
        except BaseException:
            # No cleanup function is returned on this path: release the saver and the client.
            await close()
            raise
        resource = CheckpointResource(
            self.name,
            RedisHandle(saver, client),
            RedisFinishedThreadLedger(client),
            RedisCheckpointValueStore(client, waiting * 60),
        )
        return resource, close

    def saver(self, resource: CheckpointResource) -> BaseCheckpointSaver:
        """The store's own saver."""
        return resource.handle.saver

    async def saver_with_waiting(
        self, resource: CheckpointResource, waiting_minutes: int
    ) -> tuple[BaseCheckpointSaver, CheckpointValueStore]:
        """A saver and a value store over the store's client and key space, their writes carrying ``waiting_minutes``.

        They own no client: the resource's cleanup closes the client.
        """
        from tai42_kit.llm.checkpoint import codec

        client = resource.handle.client
        view = codec.GuardedAsyncRedisSaver(redis_client=client, ttl=_ttl_config(waiting_minutes))
        await _setup(view)
        return view, RedisCheckpointValueStore(client, waiting_minutes * 60)

    def serializer(
        self, saver: BaseCheckpointSaver, compress_threshold: int | None
    ) -> tuple[Any, CheckpointCodec | None]:
        """The faithful JSON serializer and codec: the library's JSON degrades most Python values."""
        from tai42_kit.llm.checkpoint.codec import FaithfulRedisSerializer, RedisFaithfulCodec

        return FaithfulRedisSerializer(), RedisFaithfulCodec(compress_threshold)

    def value_encoding(self, saver: BaseCheckpointSaver, compress_threshold: int | None) -> RedisJsonValueEncoding:
        """The faithful JSON text, compressed above ``compress_threshold``."""
        from tai42_kit.llm.checkpoint.codec import FaithfulRedisSerializer
        from tai42_kit.llm.checkpoint.redis_values import RedisJsonValueEncoding

        return RedisJsonValueEncoding(FaithfulRedisSerializer(), compress_threshold)

    async def prepare_checkpoint(
        self, write: CheckpointWrite, checkpoint: Checkpoint, new_versions: ChannelVersions
    ) -> tuple[Checkpoint, ChannelVersions]:
        """The checkpoint encoded once as the saver's document text, unchanged channels reused from the parent's."""
        from tai42_kit.llm.checkpoint import redis_values

        return cast("Checkpoint", await redis_values.prepare_checkpoint(write, checkpoint, new_versions)), new_versions

    async def prepare_writes(
        self, write: CheckpointWrite, writes: Sequence[tuple[str, Any]]
    ) -> Sequence[tuple[str, Any]]:
        """Each write encoded once as the saver's JSON text."""
        from tai42_kit.llm.checkpoint import redis_values

        return await redis_values.prepare_writes(write, writes)

    async def recorded_generation(self, resource: CheckpointResource) -> int | None:
        """The generation the marker key holds; a marker that is not a number raises."""
        return generation_marker(await resource.handle.client.get(REDIS_FORMAT_KEY))

    async def record_generation(self, resource: CheckpointResource, generation: int) -> None:
        """Set the marker key unless it is set."""
        await resource.handle.client.set(REDIS_FORMAT_KEY, str(generation), nx=True)

    async def holds_checkpoints(self, resource: CheckpointResource) -> bool:
        """Whether the saver's checkpoint index lists a document."""
        info = await resource.handle.saver.checkpoints_index.info()
        return int(info["num_docs"]) != 0
