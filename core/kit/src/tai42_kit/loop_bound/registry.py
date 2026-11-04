"""The kit's loop-bound resource registries, kept per event loop and per client epoch as its client pools are.

A registry caches long-lived async resources (connection pools, savers, stores)
that bind to the event loop that first opened them. ``ResourceRegistry`` owns the
per-key create-once/cache lifecycle and the collect-all-errors ``close_all``;
``LoopRegistryMap`` owns the per-loop, per-epoch instance map and its accessor.
A concrete registry is a thin subclass that only supplies a cache key and a
factory coroutine.

A loop's current-epoch registry serves until the client epoch advances;
:func:`close_retired_registries` closes a retired epoch's registries on the
running loop (the serving loop's retire of a generation);
:func:`_release_registries` closes every epoch's registries of the running loop
(the loop's end, or an application's shutdown on a loop that lives on).
"""

import asyncio
import logging
import threading
from asyncio import AbstractEventLoop
from collections.abc import Awaitable, Callable
from typing import Any
from weakref import WeakKeyDictionary, WeakSet

from tai42_kit.clients.base import current_client_epoch
from tai42_kit.loop_closing import refuse_lease_on_closing_loop

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
    resource past shutdown. On a loop its end has marked closing
    (:mod:`tai42_kit.loop_closing`) a get raises ``RuntimeError`` before it
    serves or opens anything. :meth:`close_all` closes every cached resource,
    collecting failures into an ``ExceptionGroup`` so none is dropped silently.
    """

    def __init__(self) -> None:
        """Start empty and open: nothing cached, nothing closed."""
        self._resources: dict[str, tuple[Any, CleanupFn | None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()
        self._closed = False

    @property
    def has_live_resources(self) -> bool:
        """Whether any created-and-not-yet-closed resource is still cached."""
        return bool(self._resources)

    async def _get_or_init_resource(self, key: str, factory: ResourceFactory) -> Any:
        refuse_lease_on_closing_loop(asyncio.get_running_loop(), type(self).__name__, "serve a resource")
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
        """Close every cached resource and refuse later gets; every failure is raised together."""
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
    that: the current epoch's registry serves the loop until the epoch advances,
    when the next use builds a fresh one; a retired epoch's registry stays open,
    for old-epoch work on that loop still using it, until the retire closes it on
    its loop (:func:`close_retired_registries`) or the loop's release closes it
    (:func:`_release_registries`). On a loop its end has marked closing
    (:mod:`tai42_kit.loop_closing`) :meth:`current` refuses with ``RuntimeError``
    rather than build a registry nothing would release; on a loop that lives on,
    the next use after a release builds afresh. The map can be reached
    from multiple threads (each running its own loop), so every explicit access
    is serialized under a ``threading.Lock``.
    """

    def __init__(self, factory: Callable[[], R]) -> None:
        """Map each loop's epochs to registries ``factory`` builds, and join the maps a release closes."""
        self._factory = factory
        self._registries: WeakKeyDictionary[AbstractEventLoop, dict[int, R]] = WeakKeyDictionary()
        self._lock = threading.Lock()
        _LOOP_REGISTRY_MAPS.add(self)

    def _every_epoch_for_running_loop(self) -> list[R]:
        """Every epoch's registry this map holds for the running loop."""
        loop = asyncio.get_running_loop()
        with self._lock:
            return list((self._registries.get(loop) or {}).values())

    def _take_epoch_for_running_loop(self, epoch: int) -> R | None:
        """Drop ``epoch``'s registry for the running loop from the map and return it, ``None`` when there is none."""
        loop = asyncio.get_running_loop()
        with self._lock:
            per_epoch = self._registries.get(loop)
            if per_epoch is None:
                return None
            registry = per_epoch.pop(epoch, None)
            if not per_epoch:
                del self._registries[loop]
            return registry

    def current(self) -> R:
        """Return the current-epoch registry for the running loop, rebuilding after close_all.

        A closing loop is refused with ``RuntimeError``.
        """
        loop = asyncio.get_running_loop()
        refuse_lease_on_closing_loop(loop, type(self).__name__, "serve a registry")
        epoch = current_client_epoch()
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


# Every loop-registry map the kit builds; each adds itself, so a release covers any map.
_LOOP_REGISTRY_MAPS: WeakSet[LoopRegistryMap[Any]] = WeakSet()


async def _close_each(registries: list[ResourceRegistry], message: str) -> None:
    """Close every one of ``registries``, trying each after a failure; the failures raise together as ``message``."""
    errors: list[Exception] = []
    for registry in registries:
        try:
            await registry.close_all()
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise ExceptionGroup(message, errors)


async def _release_registries() -> None:
    """Close every epoch's registry of every loop-bound registry map on the running loop.

    The running loop's end, and an application's shutdown on a loop that lives on, call it
    (through :func:`~tai42_kit.loop_bound.release_loop_bound_resources`). Every registry is tried
    even after a failure; the failures are raised together as one ``ExceptionGroup``.
    """
    registries = [registry for rmap in list(_LOOP_REGISTRY_MAPS) for registry in rmap._every_epoch_for_running_loop()]
    await _close_each(registries, "errors releasing the loop's loop-bound registries")


async def close_retired_registries(epoch: int) -> None:
    """Close the retired ``epoch``'s registry of every loop-bound registry map on the running loop, and drop it.

    The retire of a generation calls it on the serving loop once the generation's admitted
    work has drained or the drain budget has run out. The current epoch is refused with
    ``ValueError``: its registries still serve. Every registry is tried even after a
    failure; the failures are raised together as one ``ExceptionGroup``.
    """
    if epoch == current_client_epoch():
        raise ValueError(f"cannot close the loop-bound registries of the current epoch {epoch}")
    taken = (rmap._take_epoch_for_running_loop(epoch) for rmap in list(_LOOP_REGISTRY_MAPS))
    await _close_each(
        [registry for registry in taken if registry is not None],
        "errors closing the retired epoch's loop-bound registries",
    )
