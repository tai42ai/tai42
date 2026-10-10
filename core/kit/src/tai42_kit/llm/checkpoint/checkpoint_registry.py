"""Per-loop registry of checkpoint savers, keyed by provider and connection string."""

from langgraph.checkpoint.base import BaseCheckpointSaver

from tai42_kit.llm._resource_registry import LoopRegistryMap, ResourceRegistry
from tai42_kit.llm.checkpoint.checkpoint import (
    CheckpointResource,
    create_checkpoint_resource,
    create_redis_saver_view,
    get_saver_from_resource,
    saver_view_waiting,
)
from tai42_kit.llm.checkpoint.ledger import FinishedThreadLedger
from tai42_kit.llm.checkpoint.retention import ThreadRetention
from tai42_kit.settings.cache_registry import register_settings_reset


class CheckpointRegistry(ResourceRegistry):
    """Per-loop cache of checkpoint resources keyed by ``provider::conn_string``.

    Wraps the create-once/close-all lifecycle from :class:`ResourceRegistry`
    around :func:`create_checkpoint_resource` (which also checks the store format),
    rebuilding the transient saver of the cached resource on each
    ``get_checkpointer`` call. A declared retention whose waiting needs its own
    saver (see :func:`saver_view_waiting`) gets a view built once per waiting value
    over the same resource and cached beside it; it owns no connection, so
    ``close_all`` just drops it.
    """

    async def resource(self, provider: str, conn_string: str | None) -> CheckpointResource:
        """Return the checkpoint resource for ``provider::conn_string``, creating it once and caching it."""
        key = f"{provider}::{conn_string}"
        return await self._get_or_init_resource(key, lambda: create_checkpoint_resource(provider, conn_string))

    async def get_checkpointer(
        self, provider: str, conn_string: str | None, retention: ThreadRetention | None = None
    ) -> BaseCheckpointSaver:
        """Return the checkpoint saver for ``provider::conn_string`` that applies ``retention``.

        ``retention`` is the thread owner's declaration; ``None`` is the platform's, served by the
        store's own saver.
        """
        resource = await self.resource(provider, conn_string)
        waiting = saver_view_waiting(provider, retention)
        if waiting is None:
            return get_saver_from_resource(provider, resource)

        async def _build_view() -> tuple[BaseCheckpointSaver, None]:
            return await create_redis_saver_view(resource, waiting), None

        return await self._get_or_init_resource(f"{provider}::{conn_string}::waiting={waiting}", _build_view)

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
