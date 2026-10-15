"""Large checkpoint values stored once per thread, applied the same way around every store's saver.

:class:`SharedValuesSaver` wraps the saver a store's registration builds. Where sharing is on, a write is
prepared by the registration (by default, by the kit's preparation for the store's channel storage),
its large values stored in the thread's value store before the saver writes; with sharing off a write
is the saver's own. A read resolves its references only when its decode announced one, whatever the
switch, so a thread written with sharing on reads back with it off. Deleting a thread deletes its
values after the saver's own delete.

A store that copies every channel value into every checkpoint (``per_checkpoint``) is handed the whole
checkpoint encoded once, its large values replaced by references; a store that keeps a channel once
per version (``per_version``) is handed each changed channel encoded once.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Collection, Coroutine, Iterator, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.serde.types import _DeltaSnapshot

from tai42_kit.llm.checkpoint.checkpoint import PreEncodedCheckpoint, PreEncodedValue
from tai42_kit.llm.checkpoint.chunking import resolve_references, resolve_tuple
from tai42_kit.llm.checkpoint.providers import CheckpointWrite
from tai42_kit.llm.checkpoint.reads import CheckpointRead, raise_failures

if TYPE_CHECKING:
    from langchain_core.runnables import RunnableConfig
    from langgraph.checkpoint.base import (
        ChannelVersions,
        Checkpoint,
        CheckpointMetadata,
        CheckpointTuple,
        DeltaChannelHistory,
    )

    from tai42_kit.llm.checkpoint.chunking import ValueEncoding
    from tai42_kit.llm.checkpoint.providers import ChannelStorage, CheckpointProvider
    from tai42_kit.llm.checkpoint.value_store import CheckpointValueStore, StoredBlob


def _thread_of(config: RunnableConfig) -> Any:
    return (config.get("configurable") or {}).get("thread_id")


# --------------------------------------------------------------------------- #
# The kit's preparations, by the store's channel storage
# --------------------------------------------------------------------------- #
async def prepare_checkpoint(
    write: CheckpointWrite, channel_storage: ChannelStorage, checkpoint: Checkpoint, new_versions: ChannelVersions
) -> tuple[Checkpoint, ChannelVersions]:
    """``checkpoint`` with its large values stored once, for a store of ``channel_storage``."""
    if channel_storage == "per_checkpoint":
        return await _prepare_whole(write, checkpoint), new_versions
    return await _prepare_per_version(write, checkpoint, new_versions), new_versions


async def _prepare_whole(write: CheckpointWrite, checkpoint: Checkpoint) -> Checkpoint:
    """Every channel chunked; a checkpoint holding a reference is handed over encoded once, whole."""

    def encode() -> tuple[Checkpoint, Mapping[str, tuple[str, bytes]]]:
        chunker = write.chunker()
        channels: dict[str, Any] = {}
        rewritten = False
        for name, value in checkpoint["channel_values"].items():
            channels[name], _encoded = chunker.chunk(value)
            rewritten = rewritten or channels[name] is not value
        if not rewritten:
            return checkpoint, chunker.blobs
        whole = {**checkpoint, "channel_values": channels}
        encoded = write.encoding.inline_form(write.encoding.encode(whole))
        return cast("Checkpoint", PreEncodedCheckpoint(whole, encoded, references=True)), chunker.blobs

    prepared, blobs = await asyncio.to_thread(encode)
    await write.store(set(blobs), blobs)
    return prepared


async def _prepare_per_version(
    write: CheckpointWrite, checkpoint: Checkpoint, new_versions: ChannelVersions
) -> Checkpoint:
    """Each changed channel chunked and handed over encoded once; a delta snapshot handed back rebuilt."""
    values = checkpoint["channel_values"]

    def encode() -> tuple[dict[str, Any], Mapping[str, tuple[str, bytes]]]:
        chunker = write.chunker()
        channels: dict[str, Any] = {}
        for name in new_versions:
            if name not in values:
                continue
            value = values[name]
            if isinstance(value, _DeltaSnapshot):
                # The saver marks a snapshot by its type: it stays one, its payload chunked in place.
                channels[name], _encoded = chunker.chunk(value)
                continue
            rewritten, encoded = chunker.chunk(value)
            channels[name] = PreEncodedValue(write.encoding.inline_form(encoded), references=rewritten is not value)
        return channels, chunker.blobs

    channels, blobs = await asyncio.to_thread(encode)
    await write.store(set(blobs), blobs)
    return cast("Checkpoint", {**checkpoint, "channel_values": {**values, **channels}})


async def prepare_writes(write: CheckpointWrite, writes: Sequence[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Every write value chunked and handed over encoded once."""

    def encode() -> tuple[list[tuple[str, Any]], Mapping[str, tuple[str, bytes]]]:
        chunker = write.chunker()
        encoded_writes: list[tuple[str, Any]] = []
        for channel, value in writes:
            rewritten, encoded = chunker.chunk(value)
            encoded_writes.append(
                (channel, PreEncodedValue(write.encoding.inline_form(encoded), references=rewritten is not value))
            )
        return encoded_writes, chunker.blobs

    encoded_writes, blobs = await asyncio.to_thread(encode)
    await write.store(set(blobs), blobs)
    return encoded_writes


# --------------------------------------------------------------------------- #
# The saver
# --------------------------------------------------------------------------- #
class SharedValuesSaver(BaseCheckpointSaver):
    """A store's saver with its large values stored once per thread; every saver the kit hands a graph is one.

    ``threshold`` ``None`` stores every value inline: a write is the inner saver's own. A read
    resolves the references its decode announced, whatever the threshold. The synchronous methods
    run the asynchronous ones on the loop the saver was built on, from another thread.
    """

    def __init__(
        self,
        inner: BaseCheckpointSaver,
        registration: CheckpointProvider,
        values: CheckpointValueStore,
        threshold: int | None,
        encoding: ValueEncoding,
    ) -> None:
        """Wrap ``inner``, the saver ``registration`` built, its large values going to ``values``."""
        self.inner = inner
        self.registration = registration
        self.values = values
        self.threshold = threshold
        self.encoding = encoding
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    @property
    def serde(self) -> Any:  # pyright: ignore[reportIncompatibleVariableOverride]
        """The inner saver's guarded serializer."""
        return self.inner.serde

    @property
    def config_specs(self) -> list:
        """The inner saver's configuration fields."""
        return self.inner.config_specs

    def get_next_version(self, current: Any, channel: None) -> Any:
        """The inner saver's next channel version."""
        return self.inner.get_next_version(current, channel)

    # -- writes --------------------------------------------------------------- #
    def _write(self, config: RunnableConfig, threshold: int) -> CheckpointWrite:
        return CheckpointWrite(
            saver=self.inner,
            config=config,
            thread_id=str((config.get("configurable") or {})["thread_id"]),
            values=self.values,
            encoding=self.encoding,
            threshold=threshold,
        )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """Store a checkpoint: its large values first, then the inner saver's write."""
        if self.threshold is None:
            return await self.inner.aput(config, checkpoint, metadata, new_versions)
        prepared, versions = await self.registration.prepare_checkpoint(
            self._write(config, self.threshold), checkpoint, new_versions
        )
        return await self.inner.aput(config, prepared, metadata, versions)

    async def aput_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        """Store pending writes: their large values first, then the inner saver's write."""
        if self.threshold is None or not writes:
            await self.inner.aput_writes(config, writes, task_id, task_path)
            return
        prepared = await self.registration.prepare_writes(self._write(config, self.threshold), writes)
        await self.inner.aput_writes(config, prepared, task_id, task_path)

    # -- reads ---------------------------------------------------------------- #
    def _fetch(self, thread_id: Any) -> Any:
        key = str(thread_id)

        async def fetch(digests: Collection[str]) -> Mapping[str, StoredBlob]:
            return await self.values.get(key, digests)

        return fetch

    async def _resolve(self, tup: CheckpointTuple) -> CheckpointTuple:
        return await resolve_tuple(tup, self.encoding, self._fetch(_thread_of(tup.config)))

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """Read a checkpoint tuple; an undecodable stored value or a missing shared value raises."""
        with CheckpointRead() as read:
            result = await self.inner.aget_tuple(config)
        raise_failures(read.failures, _thread_of(config))
        if result is None or not read.references:
            return result
        return await self._resolve(result)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 -- the library's parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """List checkpoint tuples; a tuple holding an undecodable stored value or a missing shared value raises.

        A tuple is decoded no later than it is yielded, so a reference it holds is announced no later
        than its own read step; from the first announcement on, every tuple of the read is resolved —
        a tuple without references is walked and returned as it is.
        """
        inner = cast(
            "AsyncGenerator[CheckpointTuple]", self.inner.alist(config, filter=filter, before=before, limit=limit)
        )
        announced = False
        try:
            while True:
                with CheckpointRead() as read:
                    try:
                        item = await anext(inner)
                    except StopAsyncIteration:
                        return
                raise_failures(read.failures, _thread_of(item.config))
                announced = announced or bool(read.references)
                yield await self._resolve(item) if announced else item
        finally:
            await inner.aclose()

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        """The delta channels' writes and seeds along the parent chain, their shared values resolved."""
        with CheckpointRead() as read:
            history = await self.inner.aget_delta_channel_history(config=config, channels=channels)
        thread_id = _thread_of(config)
        raise_failures(read.failures, thread_id)
        if not read.references:
            return history
        return await resolve_references(dict(history), self.encoding, self._fetch(thread_id), thread_id)

    # -- deletes and the rest of the contract --------------------------------- #
    async def adelete_thread(self, thread_id: str) -> None:
        """Delete every checkpoint and pending write of ``thread_id``, then its shared values."""
        await self.inner.adelete_thread(thread_id)
        await self.values.delete_thread(str(thread_id))

    async def adelete_for_runs(self, run_ids: Any) -> None:
        """The inner saver's delete of the runs' writes."""
        await self.inner.adelete_for_runs(run_ids)

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """The inner saver's copy; shared values are not copied to the target thread."""
        await self.inner.acopy_thread(source_thread_id, target_thread_id)

    async def aprune(self, thread_ids: Any, *, strategy: str = "keep_latest") -> None:
        """The inner saver's prune."""
        await self.inner.aprune(thread_ids, strategy=strategy)

    # -- the synchronous doors ------------------------------------------------ #
    def _check_thread(self) -> asyncio.AbstractEventLoop:
        if self._loop is None:
            raise RuntimeError(
                "the checkpoint saver was built outside a running event loop, so its synchronous methods have no "
                "loop to run on; build it on the loop (the checkpoint registry does) or use the async interface"
            )
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            raise asyncio.InvalidStateError(
                "Synchronous calls to the checkpoint saver are only allowed from a different thread. From the loop's "
                "thread, use the async interface (`await checkpointer.aget_tuple(...)`, `await graph.ainvoke(...)`)."
            )
        return self._loop

    def _run[T](self, coro: Coroutine[Any, Any, T]) -> T:
        try:
            loop = self._check_thread()
        except BaseException:
            coro.close()
            raise
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """:meth:`aget_tuple`, from another thread."""
        return self._run(self.aget_tuple(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 -- the library's parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """:meth:`alist`, from another thread, one tuple per step."""
        loop = self._check_thread()
        tuples = self.alist(config, filter=filter, before=before, limit=limit)
        while True:
            try:
                yield asyncio.run_coroutine_threadsafe(anext(tuples), loop).result()  # pyright: ignore[reportArgumentType]
            except StopAsyncIteration:
                return

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """:meth:`aput`, from another thread."""
        return self._run(self.aput(config, checkpoint, metadata, new_versions))

    def put_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        """:meth:`aput_writes`, from another thread."""
        self._run(self.aput_writes(config, writes, task_id, task_path))

    def delete_thread(self, thread_id: str) -> None:
        """:meth:`adelete_thread`, from another thread."""
        self._run(self.adelete_thread(thread_id))

    def get_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        """:meth:`aget_delta_channel_history`, from another thread."""
        return self._run(self.aget_delta_channel_history(config=config, channels=channels))

    def delete_for_runs(self, run_ids: Any) -> None:
        """:meth:`adelete_for_runs`, from another thread."""
        self._run(self.adelete_for_runs(run_ids))

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """:meth:`acopy_thread`, from another thread."""
        self._run(self.acopy_thread(source_thread_id, target_thread_id))

    def prune(self, thread_ids: Any, *, strategy: str = "keep_latest") -> None:
        """:meth:`aprune`, from another thread."""
        self._run(self.aprune(thread_ids, strategy=strategy))
