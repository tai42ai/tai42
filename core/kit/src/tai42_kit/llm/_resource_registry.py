"""Shared machinery for the loop-keyed checkpoint/store registries.

A registry caches long-lived async resources (connection pools, savers, stores)
that bind to the event loop that first opened them. ``ResourceRegistry`` owns the
per-key create-once/cache lifecycle and the collect-all-errors ``close_all``;
``LoopRegistryMap`` owns the per-loop instance map, its accessor, and the
settings-reset hook. Both the checkpoint and store registries are thin subclasses
that only supply a cache-key + a factory coroutine.
"""

import asyncio
import logging
import threading
from asyncio import AbstractEventLoop
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakKeyDictionary

logger = logging.getLogger(__name__)

CleanupFn = Callable[[], Awaitable[None]]
ResourceFactory = Callable[[], Awaitable[tuple[Any, CleanupFn | None]]]


class ResourceRegistry:
    """Create-once cache of long-lived async resources keyed by a string.

    Concrete registries expose a typed accessor that builds a cache key and a
    factory coroutine returning ``(resource, cleanup_fn)``, then delegate to
    :meth:`_get_or_init_resource`. Each key's resource is created at most once
    under a double-checked per-key lock and cached for the registry's lifetime.
    A factory that finishes after :meth:`close_all` has run closes the freshly
    opened resource and fails the get loudly rather than leaking an unmanaged
    resource past shutdown. :meth:`close_all` closes every cached resource,
    collecting failures into an ``ExceptionGroup`` so none is dropped silently.
    """

    def __init__(self) -> None:
        self._resources: dict[str, tuple[Any, CleanupFn | None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()
        self._closed = False

    @property
    def has_live_resources(self) -> bool:
        """Whether any created-and-not-yet-closed resource is still cached."""
        return bool(self._resources)

    async def _get_or_init_resource(self, key: str, factory: ResourceFactory) -> Any:
        if key in self._resources:
            return self._resources[key][0]

        async with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            lock = self._locks[key]

        async with lock:
            if key in self._resources:
                return self._resources[key][0]

            resource, closer = await factory()
            async with self._meta_lock:
                if self._closed:
                    # Shut down while this resource was being created: close what
                    # we just opened rather than leak it past close_all(), and
                    # fail the get loudly instead of returning an unmanaged
                    # resource.
                    if closer:
                        await closer()
                    raise RuntimeError(f"{type(self).__name__} is closed")
                self._resources[key] = (resource, closer)
            return resource

    async def close_all(self) -> None:
        async with self._meta_lock:
            self._closed = True
            errors: list[Exception] = []
            for key in list(self._resources.keys()):
                _, closer = self._resources[key]
                if closer:
                    try:
                        await closer()
                    except Exception as e:
                        # Keep closing the rest, but never drop a failure silently.
                        logger.exception("Error closing %s resource %s", type(self).__name__, key)
                        errors.append(e)
            self._resources.clear()
            self._locks.clear()
        if errors:
            raise ExceptionGroup(f"errors closing {type(self).__name__} resources", errors)


class LoopRegistryMap[R: ResourceRegistry]:
    """Per-event-loop map of :class:`ResourceRegistry` instances.

    Registries are keyed per event loop (their ``asyncio.Lock``s and pooled
    resources bind to the loop that first uses them) in a ``WeakKeyDictionary``,
    so a loop's registry drops when the loop is collected. The map can be reached
    from multiple threads (each running its own loop), so every explicit access
    is serialized under a ``threading.Lock``.
    """

    def __init__(self, factory: Callable[[], R], label: str) -> None:
        self._factory = factory
        self._label = label
        self._registries: WeakKeyDictionary[AbstractEventLoop, R] = WeakKeyDictionary()
        self._lock = threading.Lock()

    def current(self) -> R:
        """Return the registry for the running loop, rebuilding after close_all."""
        loop = asyncio.get_running_loop()
        with self._lock:
            registry = self._registries.get(loop)
            if registry is None or registry._closed:
                registry = self._factory()
                self._registries[loop] = registry
            return registry

    def reset(self) -> None:
        """Drop the per-loop registries so a settings reload rebuilds them.

        A registry on a still-running loop that holds live resources cannot be
        closed from this synchronous reset path — dropping it would silently
        leak its open pools. That is a caller error: ``close_all()`` must run on
        the owning loop first, so this raises loudly instead. Registries whose
        loop has already closed can never be closed cleanly (their resources are
        bound to a dead loop); those are dropped, and any that still held
        resources are logged so the drop is visible rather than silent.
        """
        with self._lock:
            live_loops = [
                loop
                for loop, registry in self._registries.items()
                if not loop.is_closed() and registry.has_live_resources
            ]
            if live_loops:
                raise RuntimeError(
                    f"{self._label}: {len(live_loops)} registry/registries on a running event loop "
                    "still hold live resources; call close_all() on the owning loop before resetting settings."
                )
            for registry in self._registries.values():
                if registry.has_live_resources:
                    logger.warning(
                        "%s: dropping registry for a closed event loop that still held %d resource(s); "
                        "its loop-bound resources cannot be closed from a settings reset.",
                        self._label,
                        len(registry._resources),
                    )
            self._registries.clear()
