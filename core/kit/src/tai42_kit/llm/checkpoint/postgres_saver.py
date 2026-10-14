"""The Postgres checkpoint saver the kit builds: shared values stored once per thread beside the saver's tables.

The library saver stores a blob per changed channel version; a large sub-value inside a channel (one
document of a dictionary of documents, one result of a dictionary of results) is stored once per
thread in the kit table ``tai42_checkpoint_values`` and the channel's blob holds a ``ref`` envelope in
its place. The values are written before the library writes the checkpoint or the pending writes that
reference them, every read resolves the references, and deleting a thread deletes its values after the
library's own delete.
"""

from __future__ import annotations

import asyncio
import zlib
from collections.abc import AsyncIterator, Collection, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, cast

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from tai42_kit.llm.checkpoint.checkpoint import PreEncodedValue
from tai42_kit.llm.checkpoint.chunking import (
    CheckpointReferenceError,
    CheckpointValueRef,
    StoredBlob,
    ValueChunker,
    resolve_references,
    resolve_tuple,
)
from tai42_kit.llm.checkpoint.codec import (
    _ZLIB_LEVEL,
    COMPRESSED_BLOB_TYPE,
    envelope_reference,
    reference_envelope,
)
from tai42_kit.llm.checkpoint.providers import checkpoint_provider_facts
from tai42_kit.llm.checkpoint.value_store import PostgresCheckpointValueStore
from tai42_kit.llm.settings import llm_provider_settings

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import (
        ChannelVersions,
        Checkpoint,
        CheckpointMetadata,
        CheckpointTuple,
        DeltaChannelHistory,
    )

_MSGPACK: Final = "msgpack"


class PostgresMsgpackValueEncoding:
    """The Postgres checkpoint store's msgpack encoding, for the chunker and the resolver.

    A value is encoded by the saver's own serializer (no codec), exactly as the library stores it.
    """

    def __init__(self, serde: Any, compress_threshold: int | None) -> None:
        """Encode through ``serde``'s inner serializer; a msgpack blob above ``compress_threshold`` is compressed."""
        self._serde = serde
        self._compress_threshold = compress_threshold

    def encode(self, value: Any) -> tuple[str, bytes]:
        """The ``(type, bytes)`` the library serializer writes for ``value``."""
        return self._serde.dumps_plain_typed(value)

    def reference(self, ref: CheckpointValueRef) -> Any:
        """The ``ref`` envelope, a plain dictionary in the msgpack blob."""
        return reference_envelope(ref)

    def as_reference(self, value: Any) -> CheckpointValueRef | None:
        """A reference as msgpack reads it back: the ``ref`` envelope dictionary."""
        return envelope_reference(value)

    def compressed(self, encoded: tuple[str, bytes]) -> tuple[str, bytes]:
        """``encoded``, zlib compressed under its own type tag when it is msgpack above the compress threshold."""
        type_, data = encoded
        if type_ == _MSGPACK and self._compress_threshold is not None and len(data) > self._compress_threshold:
            return COMPRESSED_BLOB_TYPE, zlib.compress(data, _ZLIB_LEVEL)
        return encoded

    def store_form(self, encoded: tuple[str, bytes]) -> StoredBlob:
        """The blob, compressed above the compress threshold."""
        type_, data = self.compressed(encoded)
        return StoredBlob(type_, data)

    def decode(self, blob: StoredBlob, ref: CheckpointValueRef, thread_id: Any) -> Any:
        """Decompress and decode ``blob``."""
        type_, data = blob.type, blob.data
        if type_ == COMPRESSED_BLOB_TYPE:
            try:
                data = zlib.decompress(data)
            except zlib.error as exc:
                raise CheckpointReferenceError(
                    f"checkpoint thread {thread_id!r} value {ref.digest} cannot be decompressed: {exc}"
                ) from exc
            type_ = _MSGPACK
        if len(data) != ref.size:
            raise CheckpointReferenceError(
                f"checkpoint thread {thread_id!r} value {ref.digest} is {len(data)} bytes, its reference "
                f"records {ref.size}; the thread cannot be read — delete it"
            )
        return self._serde.loads_typed((type_, data))


def _inline_primitive(value: Any) -> bool:
    # The library stores these inline in the checkpoint row, never as a blob.
    return value is None or isinstance(value, (str, int, float, bool))


class GuardedAsyncPostgresSaver(AsyncPostgresSaver):
    """The Postgres checkpoint saver the kit builds, with the thread value store beside it.

    ``conn`` is the checkpoint pool. Values are chunked off the event loop; the library stores the
    channel blobs and pending writes as they are encoded here.
    """

    def __init__(self, conn: Any, **kwargs: Any) -> None:
        """Build the library saver over ``conn`` and the thread value store over the same pool."""
        super().__init__(conn, **kwargs)
        self._values = PostgresCheckpointValueStore(conn)

    def _encoding(self) -> PostgresMsgpackValueEncoding:
        # Built per use: the kit installs its serializer guard on ``serde`` after construction.
        return PostgresMsgpackValueEncoding(self.serde, llm_provider_settings().checkpoint_inline_compress_bytes)

    def _chunker(self, encoding: PostgresMsgpackValueEncoding) -> ValueChunker:
        shared = (
            llm_provider_settings().checkpoint_shared_value_bytes
            if checkpoint_provider_facts("postgres").shared_values
            else None
        )
        return ValueChunker(encoding, shared)

    async def _store_values(
        self, thread_id: str, chunker: ValueChunker, encoding: PostgresMsgpackValueEncoding
    ) -> None:
        if not chunker.blobs:
            return
        missing = await self._values.touch(thread_id, chunker.blobs)
        if missing:
            blobs = {digest: chunker.blobs[digest] for digest in missing}
            stored = await asyncio.to_thread(lambda: {d: encoding.store_form(b) for d, b in blobs.items()})
            await self._values.put(thread_id, stored)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store a checkpoint: the shared values of its changed channels first, then the library's write."""
        thread_id = str((config.get("configurable") or {})["thread_id"])
        encoding = self._encoding()

        def encode() -> tuple[dict[str, Any], ValueChunker]:
            chunker = self._chunker(encoding)
            channels: dict[str, Any] = {}
            for name, value in checkpoint["channel_values"].items():
                if name not in new_versions:
                    continue
                if isinstance(value, _DeltaSnapshot):
                    # The library stores a snapshot under its own marker: its payload is chunked in place.
                    channels[name], _encoded = chunker.chunk(value)
                    continue
                if _inline_primitive(value) and not isinstance(value, str):
                    continue
                rewritten, encoded = chunker.chunk(value)
                if rewritten is value and _inline_primitive(value):
                    continue
                channels[name] = PreEncodedValue(encoding.compressed(encoded))
            return channels, chunker

        channels, chunker = await asyncio.to_thread(encode)
        await self._store_values(thread_id, chunker, encoding)
        rewritten = {**checkpoint, "channel_values": {**checkpoint["channel_values"], **channels}}
        return await super().aput(config, cast("Checkpoint", rewritten), metadata, new_versions)

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        """Store pending writes: their shared values first, then the library's write."""
        thread_id = str((config.get("configurable") or {})["thread_id"])
        encoding = self._encoding()

        def encode() -> tuple[list[tuple[str, Any]], ValueChunker]:
            chunker = self._chunker(encoding)
            encoded = [
                (channel, PreEncodedValue(encoding.compressed(chunker.chunk(value)[1]))) for channel, value in writes
            ]
            return encoded, chunker

        encoded, chunker = await asyncio.to_thread(encode)
        await self._store_values(thread_id, chunker, encoding)
        await super().aput_writes(config, encoded, task_id, task_path)

    async def _resolve(self, tup: CheckpointTuple) -> CheckpointTuple:
        thread_id = str((tup.config.get("configurable") or {}).get("thread_id"))

        async def fetch(digests: Collection[str]) -> Mapping[str, StoredBlob]:
            return await self._values.get(thread_id, digests)

        return await resolve_tuple(tup, self._encoding(), fetch)

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Read a checkpoint tuple with its shared values; a missing value raises."""
        result = await super().aget_tuple(config)
        return None if result is None else await self._resolve(result)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 -- the library's parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List checkpoint tuples with their shared values; a tuple holding a missing value raises."""
        async for item in super().alist(config, filter=filter, before=before, limit=limit):
            yield await self._resolve(item)

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        """The delta channels' writes and seeds along the parent chain, with their shared values resolved."""
        history = await super().aget_delta_channel_history(config=config, channels=channels)
        thread_id = str((config.get("configurable") or {})["thread_id"])

        async def fetch(digests: Collection[str]) -> Mapping[str, StoredBlob]:
            return await self._values.get(thread_id, digests)

        return await resolve_references(dict(history), self._encoding(), fetch, thread_id)

    async def adelete_thread(self, thread_id: str) -> None:
        """Delete every checkpoint and pending write of ``thread_id``, then its shared values."""
        await super().adelete_thread(thread_id)
        await self._values.delete_thread(str(thread_id))
