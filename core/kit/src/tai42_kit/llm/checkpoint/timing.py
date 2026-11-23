"""The time a saver's store waits take, recorded as spans of the trace they happen in.

:class:`TimedSaver` is the outermost layer of every saver the kit hands a graph
(:mod:`tai42_kit.llm.checkpoint.checkpoint`): each asynchronous read, write and delete is one
``checkpoint:<operation>`` span covering the whole wait the caller sees, the shared values' fetch
and store included. A span is recorded only inside a live trace, as a child of the current span; it
carries the step role ``internal``, the store's provider and the thread, never a checkpoint's values.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping, Sequence
from contextlib import aclosing, contextmanager
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    DeltaChannelHistory,
)
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import STEP_ROLE_METADATA_KEY, MonitoringLevel, SpanKind, StepRole

from tai42_kit.llm.checkpoint.thread_config import config_thread_id

_SPAN_PREFIX = "checkpoint:"
_PROVIDER_KEY = "checkpoint.provider"
_THREAD_KEY = "checkpoint.thread_id"


class TimedSaver(BaseCheckpointSaver):
    """A saver whose asynchronous store waits are spans of the live trace; every saver the kit hands a graph is one.

    With no live trace a call is the inner saver's own and records nothing. A failed call ends its span at
    ``ERROR`` with the failure's message and raises the failure. The synchronous methods are the inner saver's
    own and record nothing: they run from a thread other than the saver's loop, where no span is current.
    """

    def __init__(self, inner: BaseCheckpointSaver, *, provider: str) -> None:
        """Time ``inner``, a saver of the store ``provider`` names."""
        self.inner = inner
        self.provider = provider

    @property
    def serde(self) -> Any:  # pyright: ignore[reportIncompatibleVariableOverride]
        """The inner saver's serializer."""
        return self.inner.serde

    @property
    def config_specs(self) -> list:
        """The inner saver's configuration fields."""
        return self.inner.config_specs

    def get_next_version(self, current: Any, channel: None) -> Any:
        """The inner saver's next channel version."""
        return self.inner.get_next_version(current, channel)

    # -- the span ------------------------------------------------------------- #
    def _metadata(self, thread_id: Any) -> dict[str, Any]:
        return {
            STEP_ROLE_METADATA_KEY: StepRole.INTERNAL,
            _PROVIDER_KEY: self.provider,
            _THREAD_KEY: None if thread_id is None else str(thread_id),
        }

    @contextmanager
    def _store_wait(self, operation: str, thread_id: Any) -> Iterator[None]:
        writer = tai42_app.monitoring.active.writer
        if writer.current_trace_id() is None:
            yield
            return
        with writer.start_span(
            name=_SPAN_PREFIX + operation, kind=SpanKind.CHAIN, metadata=self._metadata(thread_id)
        ) as span:
            try:
                yield
            except Exception as exc:
                span.update(level=MonitoringLevel.ERROR, status_message=str(exc))
                raise

    # -- reads ---------------------------------------------------------------- #
    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """The inner saver's read, timed; :meth:`aget` reads through it."""
        with self._store_wait("get_tuple", config_thread_id(config)):
            return await self.inner.aget_tuple(config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 -- the library's parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        """The inner saver's listing, its items unchanged; one span covers the whole iteration.

        The span is not made current: the caller's own code runs between the items.
        """
        inner = cast(
            "AsyncGenerator[CheckpointTuple]", self.inner.alist(config, filter=filter, before=before, limit=limit)
        )
        writer = tai42_app.monitoring.active.writer
        if writer.current_trace_id() is None:
            async with aclosing(inner) as items:
                async for item in items:
                    yield item
            return
        span = writer.open_span(
            name=_SPAN_PREFIX + "list", kind=SpanKind.CHAIN, metadata=self._metadata(config_thread_id(config))
        )
        try:
            async with aclosing(inner) as items:
                async for item in items:
                    yield item
        except Exception as exc:
            span.update(level=MonitoringLevel.ERROR, status_message=str(exc))
            raise
        finally:
            span.end()

    async def aget_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        """The inner saver's delta channel history, timed."""
        with self._store_wait("delta_history", config_thread_id(config)):
            return await self.inner.aget_delta_channel_history(config=config, channels=channels)

    # -- writes --------------------------------------------------------------- #
    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """The inner saver's checkpoint write, timed."""
        with self._store_wait("put", config_thread_id(config)):
            return await self.inner.aput(config, checkpoint, metadata, new_versions)

    async def aput_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        """The inner saver's pending-writes write, timed."""
        with self._store_wait("put_writes", config_thread_id(config)):
            await self.inner.aput_writes(config, writes, task_id, task_path)

    # -- deletes and the rest of the contract --------------------------------- #
    async def adelete_thread(self, thread_id: str) -> None:
        """The inner saver's delete of ``thread_id``, timed."""
        with self._store_wait("delete_thread", thread_id):
            await self.inner.adelete_thread(thread_id)

    async def adelete_for_runs(self, run_ids: Any) -> None:
        """The inner saver's delete of the runs' writes, timed."""
        with self._store_wait("delete_for_runs", None):
            await self.inner.adelete_for_runs(run_ids)

    async def acopy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """The inner saver's copy, timed."""
        with self._store_wait("copy_thread", None):
            await self.inner.acopy_thread(source_thread_id, target_thread_id)

    async def aprune(self, thread_ids: Any, *, strategy: str = "keep_latest") -> None:
        """The inner saver's prune, timed."""
        with self._store_wait("prune", None):
            await self.inner.aprune(thread_ids, strategy=strategy)

    # -- the synchronous doors ------------------------------------------------ #
    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        """The inner saver's read."""
        return self.inner.get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 -- the library's parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        """The inner saver's listing."""
        return self.inner.list(config, filter=filter, before=before, limit=limit)

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        """The inner saver's checkpoint write."""
        return self.inner.put(config, checkpoint, metadata, new_versions)

    def put_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        """The inner saver's pending-writes write."""
        self.inner.put_writes(config, writes, task_id, task_path)

    def get_delta_channel_history(
        self, *, config: RunnableConfig, channels: Sequence[str]
    ) -> Mapping[str, DeltaChannelHistory]:
        """The inner saver's delta channel history."""
        return self.inner.get_delta_channel_history(config=config, channels=channels)

    def delete_thread(self, thread_id: str) -> None:
        """The inner saver's delete of ``thread_id``."""
        self.inner.delete_thread(thread_id)

    def delete_for_runs(self, run_ids: Any) -> None:
        """The inner saver's delete of the runs' writes."""
        self.inner.delete_for_runs(run_ids)

    def copy_thread(self, source_thread_id: str, target_thread_id: str) -> None:
        """The inner saver's copy."""
        self.inner.copy_thread(source_thread_id, target_thread_id)

    def prune(self, thread_ids: Any, *, strategy: str = "keep_latest") -> None:
        """The inner saver's prune."""
        self.inner.prune(thread_ids, strategy=strategy)
