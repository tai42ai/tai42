from langgraph.checkpoint.base import BaseCheckpointSaver

from tai42_kit.llm._resource_registry import LoopRegistryMap, ResourceRegistry
from tai42_kit.llm.checkpoint.checkpoint import create_checkpoint_resource, get_saver_from_resource
from tai42_kit.settings.cache_registry import register_settings_reset


class CheckpointRegistry(ResourceRegistry):
    """Per-loop cache of checkpoint savers keyed by ``provider::conn_string``.

    Wraps the create-once/close-all lifecycle from :class:`ResourceRegistry`
    around :func:`create_checkpoint_resource`, rebuilding the transient saver
    view of the cached resource on each ``get_checkpointer`` call.
    """

    async def get_checkpointer(self, provider: str, conn_string: str) -> BaseCheckpointSaver:
        key = f"{provider}::{conn_string}"
        resource = await self._get_or_init_resource(key, lambda: create_checkpoint_resource(provider, conn_string))
        return get_saver_from_resource(provider, resource)


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
