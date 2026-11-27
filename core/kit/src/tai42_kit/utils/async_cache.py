"""Awaited loads memoized by key: the loaded VALUE is kept, never the task or the context that loaded it.

Values live in a cachetools ``TTLCache`` (an ``LRUCache`` without a ttl), so an entry holds only what the load
returned. The values are one store for every loop and thread of the process, kept under a lock. A load in flight is
shared by the callers on its loop; a caller on another loop loads the key itself (:class:`SingleFlight`). A load that
raises keeps nothing and raises in every caller awaiting it.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Callable, Coroutine, Hashable
from typing import Any

from cachetools import Cache, LRUCache, TTLCache

from tai42_kit.utils.single_flight import SingleFlight


class AsyncCache[K: Hashable, V]:
    """Values of awaited loads by key, bounded by ``maxsize`` entries and kept ``ttl`` seconds after they land.

    ``maxsize=None`` is unbounded and ``maxsize=0`` keeps nothing; ``ttl=None`` keeps a value until it is evicted.
    An entry expires lazily: an expired value is never answered, and is dropped at the next touch of the cache.
    Every loop and thread of the process shares the values; a drop stops every loop's in-flight load of a matching
    key from being kept.
    """

    def __init__(self, *, maxsize: int | None, ttl: float | None) -> None:
        """Build an empty cache bounded by ``maxsize`` and ``ttl``."""
        size = math.inf if maxsize is None else maxsize
        self._values: Cache[K, V] = LRUCache(size) if ttl is None else TTLCache(size, ttl)
        # Guards every touch of ``_values`` and ``_drops``; a hit takes it too, since cachetools reorders on a read.
        self._lock = threading.Lock()
        self._loads: SingleFlight[K, V] = SingleFlight()
        # Counts every drop; a load keeps its value only when no drop landed since its flight started, so a load
        # in flight across an eviction answers its callers without storing what it read before the eviction.
        self._drops = 0

    async def get_or_load(self, key: K, load: Callable[[], Coroutine[Any, Any, V]]) -> V:
        """The value kept for ``key``, or the result of ``load()``, which is kept for the next caller.

        A caller on the loop of a load in flight for ``key`` joins it; a cancelled caller leaves it to the others.
        """
        with self._lock:
            try:
                return self._values[key]
            except KeyError:
                pass
        # ``SingleFlight`` calls the lambda once, as it creates the flight: the drop count is read there.
        return await self._loads.run(key, lambda: self._load_and_keep(key, load, self._drops))

    async def _load_and_keep(self, key: K, load: Callable[[], Coroutine[Any, Any, V]], drops: int) -> V:
        value = await load()
        with self._lock:
            if drops == self._drops and self._values.maxsize > 0:
                self._values[key] = value
        return value

    def invalidate(self, key: K) -> None:
        """Drop ``key``'s value; a load in flight answers its callers and keeps nothing."""
        with self._lock:
            self._drops += 1
            self._values.pop(key, None)
        self._loads.detach(lambda tracked: tracked == key)

    def evict(self, matches: Callable[[K], bool]) -> None:
        """Drop the value of every key that ``matches``; a load in flight answers its callers and keeps nothing."""
        with self._lock:
            self._drops += 1
            for key in [key for key in self._values if matches(key)]:
                # ``pop`` reads the clock once for its check and its delete: a key that expired after it was listed is
                # skipped, never raised (an expired value is never answered).
                self._values.pop(key, None)
        self._loads.detach(matches)

    def clear(self) -> None:
        """Drop every value; a load in flight answers its callers and keeps nothing."""
        with self._lock:
            self._drops += 1
            self._values.clear()
        self._loads.detach(lambda _tracked: True)

    @property
    def currsize(self) -> int:
        """How many live values the cache holds."""
        with self._lock:
            return len(self._values)

    @property
    def maxsize(self) -> int | None:
        """The entry bound, ``None`` when unbounded."""
        size = self._values.maxsize
        return None if size == math.inf else int(size)


__all__ = ["AsyncCache"]
