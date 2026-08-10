"""Tests for :class:`CacheStore`: the entry cap with LRU eviction, expired-entry
reclamation on write, monotonic-clock TTLs, the non-positive-cap guard, and the
env-configurable cap."""

from __future__ import annotations

import asyncio

import pytest
from tai42_kit.settings import reset_all_settings

import tai42_toolbox._internal.extensions.cache_store as cache_store_module
from tai42_toolbox._internal.extensions.cache_store import MISS, CacheSettings, CacheStore, cache_settings


class _Clock:
    """A controllable monotonic clock standing in for :func:`time.monotonic`."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_evicts_least_recently_used_at_cap() -> None:
    store = CacheStore(max_entries=2)
    store.write("a", 1, None)
    store.write("b", 2, None)
    # Touch "a" so "b" becomes the least-recently-used entry.
    assert store.read("a") == 1
    store.write("c", 3, None)

    assert store.read("b") is MISS
    assert store.read("a") == 1
    assert store.read("c") == 3


def test_write_reclaims_expired_entries_before_evicting(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(cache_store_module.time, "monotonic", clock)

    store = CacheStore(max_entries=2)
    store.write("a", 1, 10)  # expires at now + 10
    store.write("b", 2, None)
    clock.advance(20)  # "a" is now past its TTL

    # A third write reclaims the expired "a" rather than evicting the live "b".
    store.write("c", 3, None)

    assert store.read("a") is MISS
    assert store.read("b") == 2
    assert store.read("c") == 3


def test_write_reclaims_an_expired_non_lru_entry_and_keeps_the_live_lru(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr(cache_store_module.time, "monotonic", clock)

    store = CacheStore(max_entries=2)
    store.write("a", 1, None)  # live, no TTL, and the least-recently-used entry
    store.write("b", 2, 10)  # expires at now + 10, and the more-recently-used entry
    clock.advance(20)  # "b" is now past its TTL, "a" is still live

    # At cap, the write reclaims the expired "b" regardless of LRU position, so the live LRU "a" survives.
    store.write("c", 3, None)

    assert store.read("a") == 1
    assert store.read("b") is MISS
    assert store.read("c") == 3


def test_write_overwrite_refreshes_recency_so_a_stale_key_is_evicted_first() -> None:
    store = CacheStore(max_entries=3)
    store.write("a", 1, None)
    store.write("b", 2, None)
    store.write("c", 3, None)
    # Overwriting "a" refreshes its recency, making it most-recently-used and "b"
    # the new least-recently-used entry.
    store.write("a", 10, None)
    store.write("d", 4, None)

    # The eviction dropped the new LRU "b", not the just-overwritten "a".
    assert store.read("b") is MISS
    assert store.read("a") == 10
    assert store.read("c") == 3
    assert store.read("d") == 4


def test_ttl_checked_against_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr(cache_store_module.time, "monotonic", clock)

    store = CacheStore(max_entries=8)
    store.write("k", "v", 5)

    clock.advance(4)
    assert store.read("k") == "v"  # still within the TTL window

    clock.advance(2)  # total 6 > 5
    assert store.read("k") is MISS


def test_non_positive_cap_raises() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        CacheStore(max_entries=0)
    with pytest.raises(ValueError, match="must be positive"):
        CacheStore(max_entries=-1)


def test_eviction_skips_a_key_with_a_live_single_flight_lock() -> None:
    async def scenario() -> None:
        store = CacheStore(max_entries=2)
        store.write("a", 1, None)
        store.write("b", 2, None)
        # Hold a single-flight lock for "a": a concurrent caller is mid-flight on it.
        lock = store.key_lock("a")
        async with lock:
            # "a" is the least-recently-used entry, but its live lock protects it,
            # so the write evicts "b" instead.
            store.write("c", 3, None)
            assert store.read("a") == 1
            assert store.read("b") is MISS
            assert store.read("c") == 3

    asyncio.run(scenario())


def test_store_stays_over_cap_when_every_entry_is_locked() -> None:
    async def scenario() -> None:
        store = CacheStore(max_entries=1)
        # Two single-flight callers each hold their key's lock, so neither is evicted and the store exceeds its cap.
        async with store.key_lock("a"):
            store.write("a", 1, None)
            async with store.key_lock("b"):
                store.write("b", 2, None)
                assert store.read("a") == 1
                assert store.read("b") == 2

    asyncio.run(scenario())


def test_cap_is_env_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CACHE_MAX_ENTRIES", "3")
    reset_all_settings()
    try:
        assert CacheSettings().max_entries == 3
        assert cache_settings().max_entries == 3
        # A default-constructed store adopts the env-configured cap.
        store = CacheStore()
        store.write("a", 1, None)
        store.write("b", 2, None)
        store.write("c", 3, None)
        store.write("d", 4, None)
        assert store.read("a") is MISS
        assert store.read("d") == 4
    finally:
        reset_all_settings()
