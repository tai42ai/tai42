"""The in-process ``memory`` checkpoint store: LangGraph's in-memory saver, kept for the process's life."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, ClassVar

from tai42_kit.llm.checkpoint.ledger import as_utc, stale_threads_by_walk
from tai42_kit.llm.checkpoint.providers import (
    CheckpointProvider,
    CheckpointProviderFacts,
    register_checkpoint_provider,
)
from tai42_kit.llm.checkpoint.store_format import CHECKPOINT_STORE_FORMAT_GENERATION

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence

    from langgraph.checkpoint.base import BaseCheckpointSaver

    from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource
    from tai42_kit.llm.checkpoint.retention import ThreadRetention
    from tai42_kit.llm.checkpoint.value_encoding import MsgpackValueEncoding
    from tai42_kit.llm.checkpoint.value_store import StoredBlob


class MemoryFinishedThreadLedger:
    """The ledger of an in-process checkpoint store."""

    def __init__(self) -> None:
        """Start empty; the ledger lives as long as its store."""
        self._finished: dict[str, datetime] = {}
        self._waiting: dict[str, int] = {}

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        """Remove the finished marks of ``thread_ids`` and record (or, for ``None``, remove) their waiting."""
        for thread_id in thread_ids:
            self._finished.pop(thread_id, None)
            if retention is None:
                self._waiting.pop(thread_id, None)
            else:
                self._waiting[thread_id] = retention.waiting_minutes

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        """Record ``thread_ids`` as finished, due at ``due_at``."""
        when = as_utc(due_at)
        for thread_id in thread_ids:
            self._finished[thread_id] = when

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids due at or before ``cutoff``, earliest first."""
        bound = as_utc(cutoff)
        due = sorted((when, thread_id) for thread_id, when in self._finished.items() if when <= bound)
        return [thread_id for _, thread_id in due[:limit]]

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        for thread_id in thread_ids:
            self._finished.pop(thread_id, None)
            self._waiting.pop(thread_id, None)

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        """The recorded declared waiting of those of ``thread_ids`` that have one."""
        return {thread_id: self._waiting[thread_id] for thread_id in thread_ids if thread_id in self._waiting}

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """The threads past their waiting horizon, walked through ``saver``."""
        return await stale_threads_by_walk(saver, self.declared_waiting, now=now, default_minutes=default_minutes)


class MemoryCheckpointValueStore:
    """Shared values in a dictionary per thread, kept as long as the process."""

    def __init__(self) -> None:
        """Start empty."""
        self._threads: dict[str, dict[str, StoredBlob]] = {}

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        """The digests ``thread_id`` does not hold (a value here has no retention to re-arm)."""
        held = self._threads.get(thread_id, {})
        return {digest for digest in digests if digest not in held}

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        """Store ``blobs``; a digest already held keeps its blob."""
        held = self._threads.setdefault(thread_id, {})
        for digest, blob in blobs.items():
            held.setdefault(digest, blob)

    async def get(self, thread_id: str, digests: Collection[str]) -> dict[str, StoredBlob]:
        """The held blobs among ``digests``."""
        held = self._threads.get(thread_id, {})
        return {digest: held[digest] for digest in digests if digest in held}

    async def held_digests(self, thread_id: str) -> set[str]:
        """Every digest ``thread_id`` holds."""
        return set(self._threads.get(thread_id, {}))

    async def delete_thread(self, thread_id: str) -> None:
        """Delete every value of ``thread_id``."""
        self._threads.pop(thread_id, None)


@register_checkpoint_provider
class MemoryCheckpointProvider(CheckpointProvider):
    """LangGraph's ``InMemorySaver``: a store that starts empty with every process."""

    name: ClassVar[str] = "memory"
    facts: ClassVar[CheckpointProviderFacts] = CheckpointProviderFacts(
        durable=False, retention="process", channel_storage="per_version"
    )

    async def open(self, conn_string: str | None) -> tuple[CheckpointResource, Callable[[], Awaitable[None]]]:
        """A fresh in-memory saver, its ledger and its value store; ``conn_string`` is not used."""
        from langgraph.checkpoint.memory import InMemorySaver

        from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource

        async def close() -> None:
            return None

        resource = CheckpointResource(
            self.name, InMemorySaver(), MemoryFinishedThreadLedger(), MemoryCheckpointValueStore()
        )
        return resource, close

    def saver(self, resource: CheckpointResource) -> BaseCheckpointSaver:
        """The resource's own saver: the store is the saver."""
        return resource.handle

    def value_encoding(self, saver: BaseCheckpointSaver, compress_threshold: int | None) -> MsgpackValueEncoding:
        """The saver's msgpack, never compressed."""
        from tai42_kit.llm.checkpoint.value_encoding import MsgpackValueEncoding

        return MsgpackValueEncoding(saver.serde, None)

    async def recorded_generation(self, resource: CheckpointResource) -> int | None:
        """The current generation: a store that starts empty with its process holds nothing older."""
        return CHECKPOINT_STORE_FORMAT_GENERATION

    async def record_generation(self, resource: CheckpointResource, generation: int) -> None:
        """Nothing to record."""

    async def holds_checkpoints(self, resource: CheckpointResource) -> bool:
        """``False``: the store opened is empty."""
        return False
