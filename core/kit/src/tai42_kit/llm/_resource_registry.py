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

# How long ``drain_epoch`` sleeps between resource-release polls while waiting for
# a retired epoch's registry to go idle.
_DRAIN_POLL_SECONDS = 0.01


def _current_epoch() -> int:
    # The single process-wide epoch lives in ``clients.base``; import lazily so
    # this module (reached through ``settings`` during its import) takes no
    # import-time dependency on the clients package.
    from tai42_kit.clients.base import current_client_epoch

    return current_client_epoch()


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
    """Per-event-loop, per-epoch map of :class:`ResourceRegistry` instances.

    Registries are keyed per event loop (their ``asyncio.Lock``s and pooled
    resources bind to the loop that first uses them) in a ``WeakKeyDictionary``,
    so a loop's registries drop when the loop is collected, and per epoch under
    that: a retired epoch's registry stays reachable so an old-epoch agent stream
    keeps its resources until it releases them, while the current epoch rebuilds
    fresh. The map can be reached from multiple threads (each running its own
    loop), so every explicit access is serialized under a ``threading.Lock``.
    """

    def __init__(self, factory: Callable[[], R], label: str) -> None:
        self._factory = factory
        self._label = label
        self._registries: WeakKeyDictionary[AbstractEventLoop, dict[int, R]] = WeakKeyDictionary()
        self._lock = threading.Lock()

    def current(self) -> R:
        """Return the current-epoch registry for the running loop, rebuilding after close_all."""
        loop = asyncio.get_running_loop()
        epoch = _current_epoch()
        with self._lock:
            per_epoch = self._registries.get(loop)
            if per_epoch is None:
                per_epoch = {}
                self._registries[loop] = per_epoch
            registry = per_epoch.get(epoch)
            if registry is None or registry._closed:
                registry = self._factory()
                per_epoch[epoch] = registry
            return registry

    def reset(self) -> None:
        """Drop the current-epoch per-loop registries so a settings reload rebuilds them.

        A CURRENT-epoch registry on a still-running loop that holds live resources
        cannot be closed from this synchronous reset path — dropping it would
        silently leak its open pools. That is a caller error: ``close_all()`` must
        run on the owning loop first, so this raises loudly. RETIRED-epoch
        registries on a running loop are excluded: they drain on live-resource
        release and force-close at the drain deadline (:meth:`drain_epoch`), so a
        new epoch's reload must not refuse on them. Registries whose loop has
        already closed can never be closed cleanly (their resources bind to a dead
        loop); those are dropped at any epoch, and any that still held resources
        are logged so the drop is visible rather than silent.
        """
        current = _current_epoch()
        with self._lock:
            blocking = [
                (loop, epoch)
                for loop, per_epoch in self._registries.items()
                for epoch, registry in per_epoch.items()
                if epoch == current and not loop.is_closed() and registry.has_live_resources
            ]
            if blocking:
                raise RuntimeError(
                    f"{self._label}: {len(blocking)} registry/registries on a running event loop "
                    "still hold live resources; call close_all() on the owning loop before resetting settings."
                )
            for loop, per_epoch in list(self._registries.items()):
                for epoch, registry in list(per_epoch.items()):
                    if epoch != current and not loop.is_closed():
                        continue  # retired registry on a live loop: drain_epoch owns it
                    if registry.has_live_resources:
                        # Only a closed-loop registry can still hold resources here
                        # (a live current-epoch one raised above); its loop-bound
                        # resources cannot be closed from this sync reset.
                        logger.warning(
                            "%s: dropping registry for a closed event loop that still held %d resource(s); "
                            "its loop-bound resources cannot be closed from a settings reset.",
                            self._label,
                            len(registry._resources),
                        )
                    del per_epoch[epoch]
                if not per_epoch:
                    del self._registries[loop]

    async def drain_epoch(self, epoch: int, deadline: float) -> None:
        """Close a retired epoch's registry for the running loop, draining first.

        Waits up to ``deadline`` seconds for the registry's resources to release
        (``has_live_resources`` is the lease-equivalent), then force-closes via
        ``close_all`` — which raises an ``ExceptionGroup`` on any close failure.
        Must run on the loop that owns the registry; the current epoch cannot be
        drained.
        """
        if epoch == _current_epoch():
            raise ValueError(f"{self._label}: cannot drain the current epoch {epoch}")
        loop = asyncio.get_running_loop()
        with self._lock:
            per_epoch = self._registries.get(loop)
            registry = per_epoch.get(epoch) if per_epoch is not None else None
        if registry is None:
            return
        end = loop.time() + deadline
        while registry.has_live_resources and loop.time() < end:
            await asyncio.sleep(_DRAIN_POLL_SECONDS)
        try:
            await registry.close_all()
        finally:
            with self._lock:
                per_epoch = self._registries.get(loop)
                if per_epoch is not None:
                    per_epoch.pop(epoch, None)
                    if not per_epoch:
                        del self._registries[loop]
