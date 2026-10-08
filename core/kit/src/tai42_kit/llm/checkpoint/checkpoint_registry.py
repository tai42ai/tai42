"""Per-loop registry of checkpoint savers, keyed by provider and connection string."""

from langgraph.checkpoint.base import BaseCheckpointSaver

from tai42_kit.llm._resource_registry import LoopRegistryMap, ResourceRegistry
from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource, create_checkpoint_resource, get_saver_from_resource
from tai42_kit.llm.checkpoint.ledger import FinishedThreadLedger
from tai42_kit.settings.cache_registry import register_settings_reset


class CheckpointRegistry(ResourceRegistry):
    """Per-loop cache of checkpoint resources keyed by ``provider::conn_string``.

    Wraps the create-once/close-all lifecycle from :class:`ResourceRegistry`
    around :func:`create_checkpoint_resource` (which also checks the store format),
    rebuilding the transient saver view of the cached resource on each
    ``get_checkpointer`` call.
    """

    async def resource(self, provider: str, conn_string: str | None) -> CheckpointResource:
        """Return the checkpoint resource for ``provider::conn_string``, creating it once and caching it."""
        key = f"{provider}::{conn_string}"
        return await self._get_or_init_resource(key, lambda: create_checkpoint_resource(provider, conn_string))

    async def get_checkpointer(self, provider: str, conn_string: str | None) -> BaseCheckpointSaver:
        """Return the checkpoint saver for ``provider::conn_string``."""
        return get_saver_from_resource(provider, await self.resource(provider, conn_string))

    async def ledger(self, provider: str, conn_string: str | None) -> FinishedThreadLedger:
        """Return the finished-thread ledger of the ``provider::conn_string`` store."""
        return (await self.resource(provider, conn_string)).ledger


_registries: LoopRegistryMap[CheckpointRegistry] = LoopRegistryMap(CheckpointRegistry, "CheckpointRegistry")


def checkpoint_registry() -> CheckpointRegistry:
    """Return the CheckpointRegistry for the running event loop.

    One registry per loop; after ``close_all()`` the next call builds a fresh
    registry instead of returning the closed one.
    """
    return _registries.current()


@register_settings_reset
def _reset_checkpoint_registries() -> None:
    # Drop the per-loop registries so a settings reload rebuilds them with the
    # fresh configuration on next use. Raises if a running loop's registry still
    # holds live resources (call close_all() first) rather than leaking them.
    _registries.reset()
