"""A fixture checkpoint store registered from a plugin module: the e2e proof of the registration door.

A manifest activates it by naming this module in ``lifecycle_modules`` and setting
``LLM_PROVIDER_CHECKPOINT=probe``; the module-level :func:`register_checkpoint_provider` call fires
at import. It keeps its threads in the serving process: LangGraph's in-memory saver with the kit's
in-process ledger and value store, and a store-format marker of its own.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

from langgraph.checkpoint.base import BaseCheckpointSaver
from tai42_kit.llm.checkpoint import (
    CheckpointProvider,
    CheckpointProviderFacts,
    CheckpointResource,
    MsgpackValueEncoding,
    register_checkpoint_provider,
)
from tai42_kit.llm.checkpoint.stores.memory import MemoryCheckpointValueStore, MemoryFinishedThreadLedger


class _Handle:
    def __init__(self) -> None:
        from langgraph.checkpoint.memory import InMemorySaver

        self.saver = InMemorySaver()
        self.generation: int | None = None


@register_checkpoint_provider
class ProbeCheckpointStore(CheckpointProvider):
    """An in-process store whose waiting horizon the platform sweeps through its ledger."""

    name: ClassVar[str] = "probe"
    facts: ClassVar[CheckpointProviderFacts] = CheckpointProviderFacts(
        durable=False, retention="sweep", channel_storage="per_version"
    )

    async def open(self, conn_string: str | None) -> tuple[CheckpointResource, Callable[[], Awaitable[None]]]:
        async def close() -> None:
            return None

        resource = CheckpointResource(self.name, _Handle(), MemoryFinishedThreadLedger(), MemoryCheckpointValueStore())
        return resource, close

    def saver(self, resource: CheckpointResource) -> BaseCheckpointSaver:
        return resource.handle.saver

    def value_encoding(self, saver: BaseCheckpointSaver, compress_threshold: int | None) -> MsgpackValueEncoding:
        return MsgpackValueEncoding(saver.serde, compress_threshold)

    async def recorded_generation(self, resource: CheckpointResource) -> int | None:
        return resource.handle.generation

    async def record_generation(self, resource: CheckpointResource, generation: int) -> None:
        if resource.handle.generation is None:
            resource.handle.generation = generation

    async def holds_checkpoints(self, resource: CheckpointResource) -> bool:
        return bool(resource.handle.saver.storage)
