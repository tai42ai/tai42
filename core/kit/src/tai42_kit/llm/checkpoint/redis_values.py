"""The Redis store's value encoding and its one encoding pass, and its write preparation.

A checkpoint is encoded once, off the event loop: one faithful walk per channel whose version changed
(an unchanged channel reuses the parent checkpoint's stored form), every node's encoding measured once
for both thresholds and the digest, and the checkpoint text assembled from the channels' encodings.
Where sharing is on, a sub-value above the shared-value threshold is stored once per thread in the
thread's value store (see :mod:`tai42_kit.llm.checkpoint.chunking`) before the saver writes the
checkpoint or the pending write that references it; every write re-arms the retention of every value it
references.
"""

from __future__ import annotations

import asyncio
import base64
import zlib
from collections.abc import Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

import orjson

from tai42_kit.llm.checkpoint.checkpoint import CheckpointSerializationError, PreEncodedCheckpoint, PreEncodedValue
from tai42_kit.llm.checkpoint.chunking import CheckpointReferenceError, CheckpointValueRef, ValueChunker
from tai42_kit.llm.checkpoint.codec import FaithfulRedisSerializer, _walk
from tai42_kit.llm.checkpoint.envelope import (
    ZLIB_LEVEL,
    envelope,
    envelope_reference,
    is_kit_envelope,
    reference_envelope,
)
from tai42_kit.llm.checkpoint.reads import decode_failures, open_failures, raise_failures
from tai42_kit.llm.checkpoint.value_store import StoredBlob
from tai42_kit.utils.data.json_schema_util import MSGPACK_INT_MAX, MSGPACK_INT_MIN, find_oversized_int

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import ChannelVersions, Checkpoint

    from tai42_kit.llm.checkpoint.codec import GuardedAsyncRedisSaver
    from tai42_kit.llm.checkpoint.providers import CheckpointWrite

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
        self.serializer = serializer
        self._default = serializer._default_handler
        self.compress_threshold = compress_threshold

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
        if self.compress_threshold is not None and len(data) > self.compress_threshold:
            return StoredBlob(COMPRESSED_JSON_BLOB_TYPE, zlib.compress(data, ZLIB_LEVEL))
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
        failures, token = open_failures()
        try:
            value = self.serializer.loads_typed(("json", data))
        finally:
            decode_failures.reset(token)
        raise_failures(failures, thread_id)
        return value


def _reusable_references(stored: Any, found: set[str]) -> bool:
    """Add the digests ``stored`` (a channel as the store holds it) references; ``False`` when they are hidden.

    A compressed ``inflate`` envelope hides what it references, so a channel stored as one is walked again.
    """
    kind = type(stored)
    if kind is dict:
        if is_kit_envelope(stored):
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

    def __init__(self, encoding: RedisJsonValueEncoding) -> None:
        """Encode with ``encoding``; a value above its compress threshold is compressed inline."""
        self._serializer = encoding.serializer
        self.encoding = encoding
        self._compress_threshold = encoding.compress_threshold

    def _inline(self, data: bytes) -> bytes:
        if self._compress_threshold is None or len(data) <= self._compress_threshold:
            return data
        text = base64.b64encode(zlib.compress(data, ZLIB_LEVEL)).decode("ascii")
        return self.encoding.encode(envelope("inflate", ["json", text]))[1]

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


async def _parent_channels(
    saver: GuardedAsyncRedisSaver, config: RunnableConfig, checkpoint: Checkpoint, new_versions: ChannelVersions
) -> dict[str, Any]:
    """The parent checkpoint's stored form of each channel whose version did not change."""
    configurable = config.get("configurable") or {}
    parent_id = configurable.get("checkpoint_id")
    candidates = [name for name in checkpoint["channel_values"] if name not in new_versions]
    if not parent_id or parent_id == checkpoint["id"] or not candidates:
        return {}
    key = saver._make_redis_checkpoint_key_cached(
        configurable["thread_id"], configurable.get("checkpoint_ns", ""), parent_id
    )
    stored = await saver._redis.json().get(  # pyright: ignore[reportGeneralTypeIssues]
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
    write: CheckpointWrite, checkpoint: Checkpoint, parent: Mapping[str, Any]
) -> tuple[bytes, ValueChunker, dict[str, set[str]]]:
    """The stored text, the chunked blobs, and the digests each reused channel references."""
    reused: dict[str, Any] = {}
    reused_refs: dict[str, set[str]] = {}
    for name, stored in parent.items():
        found: set[str] = set()
        if _reusable_references(stored, found):
            reused[name] = stored
            reused_refs[name] = found
    chunker = write.chunker()
    encoder = RedisCheckpointEncoder(cast("RedisJsonValueEncoding", write.encoding))
    return encoder.encode_checkpoint(checkpoint, reused, chunker), chunker, reused_refs


async def _store_values(write: CheckpointWrite, digests: Collection[str], chunker: ValueChunker) -> set[str]:
    """Re-arm every referenced value and store those the thread lacks; return the lacking ones not chunked here."""
    if not digests:
        return set()
    missing = await write.values.touch(write.thread_id, digests)
    if not missing:
        return set()
    unknown = {digest for digest in missing if digest not in chunker.blobs}
    encoding = write.encoding
    blobs = {digest: chunker.blobs[digest] for digest in missing if digest not in unknown}
    if blobs:
        stored = await asyncio.to_thread(lambda: {d: encoding.store_form(b) for d, b in blobs.items()})
        await write.values.put(write.thread_id, stored)
    return unknown


async def prepare_checkpoint(
    write: CheckpointWrite, checkpoint: Checkpoint, new_versions: ChannelVersions
) -> PreEncodedCheckpoint:
    """``checkpoint`` encoded once, its shared values stored.

    A channel whose version is unchanged reuses the parent checkpoint's stored form; when a value it
    references is no longer held (its TTL ran out), the channel is walked again.
    """
    saver = cast("GuardedAsyncRedisSaver", write.saver)
    parent = await _parent_channels(saver, write.config, checkpoint, new_versions)
    while True:
        data, chunker, reused_refs = await asyncio.to_thread(_encode_checkpoint, write, checkpoint, parent)
        digests = set(chunker.blobs).union(*reused_refs.values())
        unknown = await _store_values(write, digests, chunker)
        if not unknown:
            return PreEncodedCheckpoint(checkpoint, ("json", data))
        stale = {name for name, refs in reused_refs.items() if refs & unknown}
        if not stale:
            raise CheckpointReferenceError(
                f"checkpoint thread {write.thread_id!r} lacks a value {min(unknown)} that no channel of this "
                "checkpoint encodes"
            )
        parent = {name: stored for name, stored in parent.items() if name not in stale}


async def prepare_writes(write: CheckpointWrite, writes: Sequence[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """``writes`` encoded once, their shared values stored."""
    encoder = RedisCheckpointEncoder(cast("RedisJsonValueEncoding", write.encoding))

    def encode() -> tuple[list[tuple[str, Any]], ValueChunker]:
        chunker = write.chunker()
        encoded = [(channel, PreEncodedValue(encoder.encode_write(value, chunker))) for channel, value in writes]
        return encoded, chunker

    encoded, chunker = await asyncio.to_thread(encode)
    await write.store(set(chunker.blobs), chunker.blobs)
    return encoded
