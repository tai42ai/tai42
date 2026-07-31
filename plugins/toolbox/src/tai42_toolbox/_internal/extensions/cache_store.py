"""Value store and key serialization for the ``cache`` tool extension.

Each ``cache`` branch owns one :class:`CacheStore` — a per-key value store with
per-key single-flight locks — so branches never share cached results. The store
is separate from the makefun factory that presents the cached tool's signature.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from typing import Any

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from tai42_kit.settings import TaiBaseSettings, settings_cache

# A miss sentinel distinct from any real cached value, so ``None`` can be cached
# and read back without being mistaken for a miss.
MISS = object()


class CacheSettings(TaiBaseSettings):
    """Bound on the number of live entries a single cache branch retains."""

    model_config = SettingsConfigDict(env_prefix="CACHE_")

    max_entries: int = Field(default=1024)


@settings_cache
def cache_settings() -> CacheSettings:
    return CacheSettings()


def compute_key(*args: Any, **kwargs: Any) -> str:
    """Serialize call arguments into a compact, stable cache key (sorted-key JSON)."""
    key_data = [args, kwargs]
    return json.dumps(key_data, sort_keys=True, default=str, separators=(",", ":"))


class CacheStore:
    """One cache branch's value store with per-key single-flight locks.

    One lock per key so identical concurrent calls single-flight instead of stampeding
    the wrapped tool; a key's lock is dropped once its value is cached. Holds at most
    ``max_entries`` live entries, evicting LRU after reclaiming expired ones, but never
    a key still holding a lock. TTLs use :func:`time.monotonic` so a wall-clock change
    never affects them.
    """

    def __init__(self, max_entries: int | None = None) -> None:
        cap = cache_settings().max_entries if max_entries is None else max_entries
        if cap <= 0:
            raise ValueError(f"CacheStore max_entries must be positive, got {cap}")
        self._max_entries = cap
        self._values: OrderedDict[str, tuple[Any, float | None]] = OrderedDict()
        self._key_locks: dict[str, asyncio.Lock] = {}

    def read(self, key: str) -> Any:
        """Return the live cached value for ``key``, or :data:`MISS` when absent
        or expired (evicting an expired entry). A hit marks ``key`` most recently
        used for LRU ordering."""
        try:
            value, expire = self._values[key]
        except KeyError:
            return MISS
        if expire is None or time.monotonic() < expire:
            self._values.move_to_end(key)
            return value
        del self._values[key]
        return MISS

    def key_lock(self, key: str) -> asyncio.Lock:
        """The single-flight lock for ``key`` (created on first use)."""
        return self._key_locks.setdefault(key, asyncio.Lock())

    def write(self, key: str, value: Any, exp: float | None) -> None:
        """Store ``value`` under ``key`` with a ``exp``-seconds TTL (``None`` or a
        non-positive ``exp`` stores it without expiry), enforcing the entry cap."""
        expire = time.monotonic() + exp if (exp is not None and exp > 0) else None
        # Reclaim expired entries before the cap check, so live ones aren't evicted first.
        self._drop_expired()
        self._values[key] = (value, expire)
        self._values.move_to_end(key)
        self._evict_to_cap()

    def drop_lock(self, key: str) -> None:
        """Drop ``key``'s single-flight lock once its value is cached."""
        self._key_locks.pop(key, None)

    def _drop_expired(self) -> None:
        """Remove every entry whose TTL has elapsed."""
        now = time.monotonic()
        expired = [key for key, (_, expire) in self._values.items() if expire is not None and expire <= now]
        for key in expired:
            del self._values[key]

    def _evict_to_cap(self) -> None:
        """Evict least-recently-used entries until within the cap.

        Keys still holding a single-flight lock are skipped; if every over-cap entry is
        locked the store stays temporarily above the cap rather than dropping an in-use value.
        """
        while len(self._values) > self._max_entries:
            victim = next((key for key in self._values if key not in self._key_locks), None)
            if victim is None:
                break
            del self._values[victim]
