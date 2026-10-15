"""A checkpoint store written against the kit's public checkpoint seam only: the neutral fifth saver.

``probe`` stores every checkpoint whole, one serializer call per checkpoint (its channel values copied
into every checkpoint); ``probe-versioned`` stores a channel value once per version, one serializer
call per changed channel. Both keep their data in this process, per connection string, so a test can
open the same store again. Nothing here names a kit internal: a store any plugin could register.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_metadata,
)

from tai42_kit.llm.checkpoint import (
    CheckpointProvider,
    CheckpointProviderFacts,
    CheckpointResource,
    MsgpackValueEncoding,
    StoredBlob,
    ThreadRetention,
    as_utc,
    register_checkpoint_provider,
    stale_threads_by_walk,
)

Typed = tuple[str, bytes]


def _ids(config: RunnableConfig) -> tuple[str, str, str | None]:
    configurable: dict[str, Any] = dict(config.get("configurable") or {})
    return str(configurable["thread_id"]), configurable.get("checkpoint_ns", ""), configurable.get("checkpoint_id")


class ProbeSaver(BaseCheckpointSaver[int]):
    """A saver that stores each checkpoint whole, its channel values inside it."""

    def __init__(self) -> None:
        super().__init__()
        # thread -> namespace -> checkpoint id -> (checkpoint, metadata, parent id)
        self.checkpoints: dict[str, dict[str, dict[str, tuple[Typed, Typed, str | None]]]] = defaultdict(
            lambda: defaultdict(dict)
        )
        # (thread, namespace, checkpoint id) -> (task id, index) -> (channel, value)
        self.writes: dict[tuple[str, str, str], dict[tuple[str, int], tuple[str, Typed]]] = defaultdict(dict)

    def _dump_checkpoint(self, checkpoint: Checkpoint, new_versions: ChannelVersions, thread: str, ns: str) -> Typed:
        return self.serde.dumps_typed(checkpoint)

    def _load_checkpoint(self, typed: Typed, thread: str, ns: str) -> Checkpoint:
        return self.serde.loads_typed(typed)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread, ns, parent = _ids(config)
        typed = self._dump_checkpoint(checkpoint, new_versions, thread, ns)
        meta = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        self.checkpoints[thread][ns][checkpoint["id"]] = (typed, meta, parent)
        return {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": checkpoint["id"]}}

    async def aput_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        thread, ns, checkpoint_id = _ids(config)
        held = self.writes[(thread, ns, str(checkpoint_id))]
        for index, (channel, value) in enumerate(writes):
            held[(task_id, index)] = (channel, self.serde.dumps_typed(value))

    def _tuple(self, thread: str, ns: str, checkpoint_id: str) -> CheckpointTuple:
        typed, meta, parent = self.checkpoints[thread][ns][checkpoint_id]
        writes = self.writes.get((thread, ns, checkpoint_id), {})
        return CheckpointTuple(
            config={"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}},
            checkpoint=self._load_checkpoint(typed, thread, ns),
            metadata=self.serde.loads_typed(meta),
            parent_config=(
                None
                if parent is None
                else {"configurable": {"thread_id": thread, "checkpoint_ns": ns, "checkpoint_id": parent}}
            ),
            pending_writes=[
                (task_id, channel, self.serde.loads_typed(value))
                for (task_id, _index), (channel, value) in sorted(writes.items())
            ],
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread, ns, checkpoint_id = _ids(config)
        held = self.checkpoints.get(thread, {}).get(ns, {})
        if checkpoint_id is None:
            if not held:
                return None
            checkpoint_id = max(held)
        if checkpoint_id not in held:
            return None
        return self._tuple(thread, ns, checkpoint_id)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,  # noqa: A002 -- the library's parameter name
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        threads = sorted(self.checkpoints) if config is None else [_ids(config)[0]]
        yielded = 0
        for thread in threads:
            for ns, held in sorted(self.checkpoints.get(thread, {}).items()):
                for checkpoint_id in sorted(held, reverse=True):
                    if before is not None and checkpoint_id >= str(_ids(before)[2]):
                        continue
                    if limit is not None and yielded >= limit:
                        return
                    yielded += 1
                    yield self._tuple(thread, ns, checkpoint_id)

    async def adelete_thread(self, thread_id: str) -> None:
        self.checkpoints.pop(str(thread_id), None)
        for key in [key for key in self.writes if key[0] == str(thread_id)]:
            del self.writes[key]


class VersionedProbeSaver(ProbeSaver):
    """A saver that stores a channel value once per version, one serializer call per changed channel."""

    def __init__(self) -> None:
        super().__init__()
        self.blobs: dict[tuple[str, str, str, Any], Typed] = {}

    def _dump_checkpoint(self, checkpoint: Checkpoint, new_versions: ChannelVersions, thread: str, ns: str) -> Typed:
        values = checkpoint["channel_values"]
        for channel, version in new_versions.items():
            if channel in values:
                self.blobs[(thread, ns, channel, version)] = self.serde.dumps_typed(values[channel])
        return self.serde.dumps_typed({**checkpoint, "channel_values": {}})

    def _load_checkpoint(self, typed: Typed, thread: str, ns: str) -> Checkpoint:
        checkpoint = self.serde.loads_typed(typed)
        checkpoint["channel_values"] = {
            channel: self.serde.loads_typed(self.blobs[(thread, ns, channel, version)])
            for channel, version in checkpoint["channel_versions"].items()
            if (thread, ns, channel, version) in self.blobs
        }
        return checkpoint

    async def adelete_thread(self, thread_id: str) -> None:
        await super().adelete_thread(thread_id)
        for key in [key for key in self.blobs if key[0] == str(thread_id)]:
            del self.blobs[key]


class ProbeValueStore:
    """Shared values in a dictionary per thread."""

    def __init__(self) -> None:
        self.threads: dict[str, dict[str, StoredBlob]] = defaultdict(dict)
        self.calls: list[str] = []

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        self.calls.append("touch")
        return {digest for digest in digests if digest not in self.threads.get(thread_id, {})}

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        self.calls.append("put")
        self.threads[thread_id].update(blobs)

    async def get(self, thread_id: str, digests: Collection[str]) -> dict[str, StoredBlob]:
        held = self.threads.get(thread_id, {})
        return {digest: held[digest] for digest in digests if digest in held}

    async def held_digests(self, thread_id: str) -> set[str]:
        return set(self.threads.get(thread_id, {}))

    async def delete_thread(self, thread_id: str) -> None:
        self.threads.pop(thread_id, None)


class ProbeLedger:
    """A finished-thread ledger over dictionaries, its stale threads found by walking the saver."""

    def __init__(self) -> None:
        self.finished: dict[str, datetime] = {}
        self.waiting: dict[str, int] = {}

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        for thread_id in thread_ids:
            self.finished.pop(thread_id, None)
            if retention is None:
                self.waiting.pop(thread_id, None)
            else:
                self.waiting[thread_id] = retention.waiting_minutes

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        for thread_id in thread_ids:
            self.finished[thread_id] = as_utc(due_at)

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        due = sorted((when, thread_id) for thread_id, when in self.finished.items() if when <= as_utc(cutoff))
        return [thread_id for _when, thread_id in due[:limit]]

    async def forget(self, thread_ids: Sequence[str]) -> None:
        for thread_id in thread_ids:
            self.finished.pop(thread_id, None)
            self.waiting.pop(thread_id, None)

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        return {thread_id: self.waiting[thread_id] for thread_id in thread_ids if thread_id in self.waiting}

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        return await stale_threads_by_walk(saver, self.declared_waiting, now=now, default_minutes=default_minutes)


@dataclass
class ProbeHandle:
    """One probe store: its saver, its ledger, its value store and its format marker."""

    saver: ProbeSaver
    ledger: ProbeLedger = field(default_factory=ProbeLedger)
    values: ProbeValueStore = field(default_factory=ProbeValueStore)
    marker: dict[str, int] = field(default_factory=dict)


STORES: dict[tuple[str, str | None], ProbeHandle] = {}


class _ProbeProvider(CheckpointProvider):
    saver_class: ClassVar[type[ProbeSaver]] = ProbeSaver

    async def open(self, conn_string: str | None) -> tuple[CheckpointResource, Callable[[], Awaitable[None]]]:
        handle = STORES.get((self.name, conn_string))
        if handle is None:
            handle = STORES[(self.name, conn_string)] = ProbeHandle(self.saver_class())

        async def close() -> None:
            return None

        return CheckpointResource(self.name, handle, handle.ledger, handle.values), close

    def saver(self, resource: CheckpointResource) -> BaseCheckpointSaver:
        return resource.handle.saver

    def value_encoding(self, saver: BaseCheckpointSaver, compress_threshold: int | None) -> MsgpackValueEncoding:
        return MsgpackValueEncoding(saver.serde, compress_threshold)

    async def recorded_generation(self, resource: CheckpointResource) -> int | None:
        return resource.handle.marker.get("generation")

    async def record_generation(self, resource: CheckpointResource, generation: int) -> None:
        resource.handle.marker.setdefault("generation", generation)

    async def holds_checkpoints(self, resource: CheckpointResource) -> bool:
        return bool(resource.handle.saver.checkpoints)


@register_checkpoint_provider
class ProbeProvider(_ProbeProvider):
    """The probe store, its channel values copied into every checkpoint."""

    name: ClassVar[str] = "probe"
    facts: ClassVar[CheckpointProviderFacts] = CheckpointProviderFacts(
        durable=False, retention="sweep", channel_storage="per_checkpoint"
    )


@register_checkpoint_provider
class VersionedProbeProvider(_ProbeProvider):
    """The probe store, a channel value kept once per version."""

    name: ClassVar[str] = "probe-versioned"
    facts: ClassVar[CheckpointProviderFacts] = CheckpointProviderFacts(
        durable=False, retention="sweep", channel_storage="per_version"
    )
    saver_class: ClassVar[type[ProbeSaver]] = VersionedProbeSaver
