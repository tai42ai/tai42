"""The Redis saver's shared values and its one encoding pass.

A checkpoint is encoded once, off the event loop: one faithful walk per channel whose version changed
(an unchanged channel reuses the parent checkpoint's stored form), every node's encoding measured once
for both thresholds and the digest, and the checkpoint text assembled from the channels' encodings.
Where sharing is on, a sub-value above the saver's shared-value threshold is stored once per thread in
the saver's value store (see :mod:`tai42_kit.llm.checkpoint.chunking`), before the library writes the
checkpoint or the pending write that references it; every write re-arms the retention of every value it
references, and a read that decoded a reference resolves it.
"""

from __future__ import annotations

import asyncio
import base64
import zlib
from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

import orjson

from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError, PreEncodedCheckpoint, PreEncodedValue
from tai42_kit.llm.checkpoint.chunking import (
    CheckpointReferenceError,
    CheckpointValueRef,
    StoredBlob,
    ValueChunker,
    resolve_tuple,
)
from tai42_kit.llm.checkpoint.codec import (
    _ZLIB_LEVEL,
    FaithfulRedisSerializer,
    _decode_failures,
    _envelope,
    _is_kit_envelope,
    _open_failures,
    _raise_failures,
    _walk,
    envelope_reference,
    reference_envelope,
)
from tai42_kit.llm.checkpoint.value_store import RedisCheckpointValueStore
from tai42_kit.llm.settings import llm_provider_settings
from tai42_kit.utils.data.json_schema_util import MSGPACK_INT_MAX, MSGPACK_INT_MIN, find_oversized_int

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointTuple

    from tai42_kit.llm.checkpoint.codec import GuardedAsyncRedisSaver

# The type tags of a shared value's blob in the Redis value store.
JSON_BLOB_TYPE: Final = "json"
COMPRESSED_JSON_BLOB_TYPE: Final = "json+zlib"


class RedisJsonValueEncoding:
    """The Redis checkpoint store's JSON encoding of a faithfully rewritten value, for the chunker and resolver.

    A value is encoded once by orjson with the library serializer's own default handler, which writes
    the library's markers for its ``Interrupt``, ``Send`` and ``_DeltaSnapshot`` objects: the same text
    the library serializer writes for a rewritten value.
    """

    def __init__(self, serializer: FaithfulRedisSerializer, compress_threshold: int | None) -> None:
        """Encode with ``serializer``'s default handler; a blob above ``compress_threshold`` bytes is compressed."""
        self._serializer = serializer
        self._default = serializer._default_handler
        self._compress_threshold = compress_threshold

    def encode(self, value: Any) -> tuple[str, bytes]:
        """The JSON text of a rewritten ``value``."""
        try:
            return "json", orjson.dumps(value, default=self._default, option=orjson.OPT_PASSTHROUGH_DATACLASS)
        except (TypeError, orjson.JSONEncodeError) as exc:
            # orjson encodes the integers of the native msgpack range and no others.
            overflow = find_oversized_int(value, minimum=MSGPACK_INT_MIN, maximum=MSGPACK_INT_MAX)
            if overflow is not None:
                path, number = overflow
                raise CheckpointSerializationError(
                    f"checkpoint value at {path} = {number} exceeds the integer range "
                    f"[{MSGPACK_INT_MIN}, {MSGPACK_INT_MAX}] the redis checkpointer stores"
                ) from exc
            raise CheckpointSerializationError(
                f"checkpoint value could not be JSON-encoded after the faithful rewrite: {exc}"
            ) from exc

    def reference(self, ref: CheckpointValueRef) -> Any:
        """The ``ref`` envelope."""
        return reference_envelope(ref)

    def as_reference(self, value: Any) -> CheckpointValueRef | None:
        """A reference as the reviver reads it back."""
        return value if isinstance(value, CheckpointValueRef) else None

    def store_form(self, encoded: tuple[str, bytes]) -> StoredBlob:
        """The JSON text, zlib compressed above the compress threshold."""
        data = encoded[1]
        if self._compress_threshold is not None and len(data) > self._compress_threshold:
            return StoredBlob(COMPRESSED_JSON_BLOB_TYPE, zlib.compress(data, _ZLIB_LEVEL))
        return StoredBlob(JSON_BLOB_TYPE, data)

    def decode(self, blob: StoredBlob, ref: CheckpointValueRef, thread_id: Any) -> Any:
        """Decompress and revive ``blob``; a stored value that cannot be read raises."""
        if blob.type == COMPRESSED_JSON_BLOB_TYPE:
            try:
                data = zlib.decompress(blob.data)
            except zlib.error as exc:
                raise CheckpointReferenceError(
                    f"checkpoint thread {thread_id!r} value {ref.digest} cannot be decompressed: {exc}"
                ) from exc
        elif blob.type == JSON_BLOB_TYPE:
            data = blob.data
        else:
            raise CheckpointReferenceError(
                f"checkpoint thread {thread_id!r} value {ref.digest} has an unknown type {blob.type!r}"
            )
        if len(data) != ref.size:
            raise CheckpointReferenceError(
                f"checkpoint thread {thread_id!r} value {ref.digest} is {len(data)} bytes, its reference "
                f"records {ref.size}; the thread cannot be read — delete it"
            )
        failures, token = _open_failures()
        try:
            value = self._serializer.loads_typed(("json", data))
        finally:
            _decode_failures.reset(token)
        _raise_failures(failures, thread_id)
        return value


def _reusable_references(stored: Any, found: set[str]) -> bool:
    """Add the digests ``stored`` (a channel as the store holds it) references; ``False`` when they are hidden.

    A compressed ``inflate`` envelope hides what it references, so a channel stored as one is walked again.
    """
    kind = type(stored)
    if kind is dict:
        if _is_kit_envelope(stored):
            ref = envelope_reference(stored)
            if ref is not None:
                found.add(ref.digest)
                found.update(ref.closure)
                return True
            args = stored.get("args")
            return not (isinstance(args, list) and args and args[0] == "inflate")
        return all(_reusable_references(item, found) for item in stored.values())
    if kind is list:
        return all(_reusable_references(item, found) for item in stored)
    return True


class RedisCheckpointEncoder:
    """The Redis store's one encoding pass: the faithful walk, the shared-value chunking, the inline compression."""

    def __init__(self, shared_threshold: int | None, compress_threshold: int | None) -> None:
        """Share sub-values above ``shared_threshold`` bytes, compress inline above ``compress_threshold``."""
        self._serializer = FaithfulRedisSerializer()
        self.encoding = RedisJsonValueEncoding(self._serializer, compress_threshold)
        self._shared_threshold = shared_threshold
        self._compress_threshold = compress_threshold

    def chunker(self) -> ValueChunker:
        """A chunker over this store's encoding and shared-value threshold."""
        return ValueChunker(self.encoding, self._shared_threshold)

    def _inline(self, data: bytes) -> bytes:
        if self._compress_threshold is None or len(data) <= self._compress_threshold:
            return data
        text = base64.b64encode(zlib.compress(data, _ZLIB_LEVEL)).decode("ascii")
        return self.encoding.encode(_envelope("inflate", ["json", text]))[1]

    def encode_value(self, value: Any, path: str, chunker: ValueChunker) -> bytes:
        """The stored JSON text of one channel value or pending write."""
        _rewritten, (_type, data) = chunker.chunk(_walk(value, path))
        return self._inline(data)

    def encode_write(self, value: Any, chunker: ValueChunker) -> tuple[str, bytes]:
        """The ``(type, bytes)`` a pending write is stored as."""
        if value is None or isinstance(value, (bytes, bytearray)):
            # Stored by the library serializer's own null and bytes types.
            return self._serializer.dumps_typed(value)
        return "json", self.encode_value(value, "$", chunker)

    def encode_checkpoint(
        self, checkpoint: Mapping[str, Any], reused: Mapping[str, Any], chunker: ValueChunker
    ) -> bytes:
        """The stored JSON text of ``checkpoint``; a channel in ``reused`` keeps that stored form as it is."""
        rewritten: dict[str, Any] = {
            key: _walk(value, f"$.{key}") for key, value in checkpoint.items() if key != "channel_values"
        }
        channels: dict[str, Any] = {}
        for name, value in checkpoint["channel_values"].items():
            if name in reused:
                channels[name] = reused[name]
            else:
                channels[name] = orjson.Fragment(self.encode_value(value, f"$.channel_values.{name}", chunker))
        rewritten["channel_values"] = channels
        return self.encoding.encode(rewritten)[1]


class RedisSharedValues:
    """The shared values of one Redis saver: its one-pass encoding, its value store, the reads' resolution.

    The value store writes through the saver's client and stamps the saver's waiting retention.
    """

    def __init__(self, saver: GuardedAsyncRedisSaver, shared_value_threshold: int | None) -> None:
        """Serve ``saver``, sharing sub-values above ``shared_value_threshold`` bytes; ``None`` shares none."""
        self._saver = saver
        self.encoder = RedisCheckpointEncoder(
            shared_value_threshold, llm_provider_settings().checkpoint_inline_compress_bytes
        )
        ttl_minutes = (saver.ttl_config or {}).get("default_ttl")
        self.store = RedisCheckpointValueStore(saver._redis, None if ttl_minutes is None else int(ttl_minutes * 60))

    async def _parent_channels(
        self, config: RunnableConfig, checkpoint: Checkpoint, new_versions: ChannelVersions
    ) -> dict[str, Any]:
        """The parent checkpoint's stored form of each channel whose version did not change."""
        configurable = config.get("configurable") or {}
        parent_id = configurable.get("checkpoint_id")
        candidates = [name for name in checkpoint["channel_values"] if name not in new_versions]
        if not parent_id or parent_id == checkpoint["id"] or not candidates:
            return {}
        key = self._saver._make_redis_checkpoint_key_cached(
            configurable["thread_id"], configurable.get("checkpoint_ns", ""), parent_id
        )
        stored = await self._saver._redis.json().get(  # pyright: ignore[reportGeneralTypeIssues]
            key, "$.checkpoint.channel_values", "$.checkpoint.channel_versions"
        )
        if not stored:
            return {}
        values = (stored.get("$.checkpoint.channel_values") or [None])[0] or {}
        versions = (stored.get("$.checkpoint.channel_versions") or [None])[0] or {}
        current = checkpoint["channel_versions"]
        return {
            name: values[name]
            for name in candidates
            if name in values and name in current and versions.get(name) == str(current[name])
        }

    def _encode_checkpoint(
        self, checkpoint: Checkpoint, parent: Mapping[str, Any]
    ) -> tuple[bytes, ValueChunker, dict[str, set[str]]]:
        """The stored text, the chunked blobs, and the digests each reused channel references."""
        reused: dict[str, Any] = {}
        reused_refs: dict[str, set[str]] = {}
        for name, stored in parent.items():
            found: set[str] = set()
            if _reusable_references(stored, found):
                reused[name] = stored
                reused_refs[name] = found
        chunker = self.encoder.chunker()
        return self.encoder.encode_checkpoint(checkpoint, reused, chunker), chunker, reused_refs

    async def _store_values(self, thread_id: str, digests: Collection[str], chunker: ValueChunker) -> set[str]:
        """Re-arm every referenced value and store those the thread lacks; return the lacking ones not chunked here."""
        if not digests:
            return set()
        missing = await self.store.touch(thread_id, digests)
        if not missing:
            return set()
        unknown = {digest for digest in missing if digest not in chunker.blobs}
        encoding = self.encoder.encoding
        blobs = {digest: chunker.blobs[digest] for digest in missing if digest not in unknown}
        if blobs:
            stored = await asyncio.to_thread(lambda: {d: encoding.store_form(b) for d, b in blobs.items()})
            await self.store.put(thread_id, stored)
        return unknown

    async def prepare_checkpoint(
        self, config: RunnableConfig, checkpoint: Checkpoint, new_versions: ChannelVersions
    ) -> PreEncodedCheckpoint:
        """``checkpoint`` encoded, its shared values stored.

        A channel whose version is unchanged reuses the parent checkpoint's stored form; when a value it
        references is no longer held, the channel is walked again.
        """
        thread_id = str((config.get("configurable") or {})["thread_id"])
        parent = await self._parent_channels(config, checkpoint, new_versions)
        while True:
            data, chunker, reused_refs = await asyncio.to_thread(self._encode_checkpoint, checkpoint, parent)
            digests = set(chunker.blobs).union(*reused_refs.values())
            unknown = await self._store_values(thread_id, digests, chunker)
            if not unknown:
                return PreEncodedCheckpoint(checkpoint, ("json", data))
            stale = {name for name, refs in reused_refs.items() if refs & unknown}
            if not stale:
                raise CheckpointReferenceError(
                    f"checkpoint thread {thread_id!r} lacks a value {min(unknown)} that no channel of this "
                    "checkpoint encodes"
                )
            parent = {name: stored for name, stored in parent.items() if name not in stale}

    async def prepare_writes(self, config: RunnableConfig, writes: Sequence[tuple[str, Any]]) -> list[tuple[str, Any]]:
        """``writes`` encoded, their shared values stored."""
        thread_id = str((config.get("configurable") or {})["thread_id"])

        def encode() -> tuple[list[tuple[str, Any]], ValueChunker]:
            chunker = self.encoder.chunker()
            encoded = [
                (channel, PreEncodedValue(self.encoder.encode_write(value, chunker))) for channel, value in writes
            ]
            return encoded, chunker

        encoded, chunker = await asyncio.to_thread(encode)
        await self._store_values(thread_id, set(chunker.blobs), chunker)
        return encoded

    async def resolve(self, tup: CheckpointTuple) -> CheckpointTuple:
        """``tup`` with every reference resolved from the thread's value store."""
        thread_id = str((tup.config.get("configurable") or {}).get("thread_id"))

        async def fetch(digests: Collection[str]) -> Mapping[str, StoredBlob]:
            return await self.store.get(thread_id, digests)

        return await resolve_tuple(tup, self.encoder.encoding, fetch)

    async def delete_thread(self, thread_id: str) -> None:
        """Delete every shared value of ``thread_id``."""
        await self.store.delete_thread(thread_id)
