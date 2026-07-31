import json
from typing import Any

from langgraph.store.base import BaseStore

from tai42_kit.llm._resource_registry import LoopRegistryMap, ResourceRegistry
from tai42_kit.llm.store.store import create_store_resource, get_store_from_resource
from tai42_kit.settings.cache_registry import register_settings_reset


class StoreRegistry(ResourceRegistry):
    """Per-loop cache of long-lived store resources (connection pools/stores).

    Wraps the create-once/close-all lifecycle from :class:`ResourceRegistry`
    around :func:`create_store_resource`.

    ARCHITECTURAL NOTE ON CACHE KEYS:
    For postgres the cached resource is a connection pool that is agnostic to
    the embedding model: 'index' (and the other kwargs) are re-applied on the
    transient ``AsyncPostgresStore`` built per call, so 'index' is excluded
    from the key. For every other provider the cached resource IS the store,
    with 'index' baked in — so 'index' must be part of the key or two callers
    differing only in embedding config would silently share one store.
    Non-JSON-serializable values inside 'index' (e.g. embedding objects) are
    fingerprinted via ``repr`` — stable within a process, which is the cache's
    lifetime.
    """

    def _generate_key(self, provider: str, conn_string: str, kwargs: dict[str, Any]) -> str:
        clean_kwargs = kwargs.copy()
        if provider == "postgres":
            clean_kwargs.pop("index", None)
        config_str = json.dumps(clean_kwargs, sort_keys=True, default=repr)
        return f"{provider}::{conn_string}::{config_str}"

    async def get_store(self, provider: str, conn_string: str, **kwargs) -> BaseStore:
        key = self._generate_key(provider, conn_string, kwargs)
        resource = await self._get_or_init_resource(key, lambda: create_store_resource(provider, conn_string, **kwargs))
        return get_store_from_resource(provider, resource, **kwargs)


_registries: LoopRegistryMap[StoreRegistry] = LoopRegistryMap(StoreRegistry, "StoreRegistry")


def store_registry() -> StoreRegistry:
    """Return the StoreRegistry for the running event loop.

    One registry per loop; after ``close_all()`` the next call builds a fresh
    registry instead of returning the closed one.
    """
    return _registries.current()


@register_settings_reset
def _reset_store_registries() -> None:
    # Drop the per-loop registries so a settings reload rebuilds them with the
    # fresh configuration on next use. Raises if a running loop's registry still
    # holds live resources (call close_all() first) rather than leaking them.
    _registries.reset()
