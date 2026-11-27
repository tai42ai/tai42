"""``AsyncCache``: awaited loads memoized by key, bounded by size and ttl, one load per key while it is in flight."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from cachetools import TTLCache

from tai42_kit.utils import async_cache
from tai42_kit.utils.async_cache import AsyncCache

_caller_scope: contextvars.ContextVar[str] = contextvars.ContextVar("caller_scope", default="none")


class _Loads:
    """A loader that answers ``value-<key>`` and records every key it loaded."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def of(self, key: str):
        async def load() -> str:
            self.keys.append(key)
            return f"value-{key}"

        return load


async def test_a_loaded_value_answers_the_next_caller_without_a_load() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=8, ttl=60)
    loads = _Loads()

    assert await cache.get_or_load("a", loads.of("a")) == "value-a"
    assert await cache.get_or_load("a", loads.of("a")) == "value-a"
    assert loads.keys == ["a"]
    assert cache.currsize == 1
    assert cache.maxsize == 8


async def test_concurrent_callers_of_one_key_share_one_load() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=8, ttl=60)
    release = asyncio.Event()
    started: list[str] = []

    async def load() -> str:
        started.append("a")
        await release.wait()
        return "value-a"

    callers = [asyncio.create_task(cache.get_or_load("a", load)) for _ in range(3)]
    await asyncio.sleep(0)
    release.set()

    assert await asyncio.gather(*callers) == ["value-a"] * 3
    assert started == ["a"]


async def test_a_caller_arriving_after_the_landing_and_before_the_first_waiter_resumes_gets_the_value() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=8, ttl=60)
    loads = _Loads()
    late: list[asyncio.Task[str]] = []
    first_done_then: list[bool] = []

    async def load() -> str:
        loads.keys.append("a")

        def arrive_late() -> None:
            first_done_then.append(first.done())
            late.append(asyncio.ensure_future(cache.get_or_load("a", loads.of("a"))))

        # Queued while the load's step runs: it fires once the load has landed, before the first waiter wakes.
        asyncio.get_running_loop().call_soon(arrive_late)
        return "value-a"

    first = asyncio.create_task(cache.get_or_load("a", load))

    assert await first == "value-a"
    assert first_done_then == [False]
    assert await late[0] == "value-a"
    assert loads.keys == ["a"]


async def test_a_raising_load_keeps_nothing_and_raises_in_every_waiter() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=8, ttl=60)
    release = asyncio.Event()
    attempts: list[str] = []

    async def failing() -> str:
        attempts.append("a")
        await release.wait()
        raise RuntimeError("load failed")

    callers = [asyncio.create_task(cache.get_or_load("a", failing)) for _ in range(2)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*callers, return_exceptions=True)

    assert [str(result) for result in results] == ["load failed", "load failed"]
    assert all(isinstance(result, RuntimeError) for result in results)
    assert attempts == ["a"]
    assert cache.currsize == 0
    assert await cache.get_or_load("a", _Loads().of("a")) == "value-a"


async def test_a_value_past_its_ttl_is_loaded_again() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=8, ttl=0.2)
    loads = _Loads()

    await cache.get_or_load("a", loads.of("a"))
    await asyncio.sleep(0.3)

    assert cache.currsize == 0
    await cache.get_or_load("a", loads.of("a"))
    assert loads.keys == ["a", "a"]


async def test_the_least_recently_used_value_leaves_a_full_cache() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=2, ttl=None)
    loads = _Loads()

    await cache.get_or_load("a", loads.of("a"))
    await cache.get_or_load("b", loads.of("b"))
    await cache.get_or_load("a", loads.of("a"))
    await cache.get_or_load("c", loads.of("c"))  # evicts b, the least recently used

    assert cache.currsize == 2
    await cache.get_or_load("a", loads.of("a"))
    await cache.get_or_load("b", loads.of("b"))
    assert loads.keys == ["a", "b", "c", "b"]


async def test_an_unbounded_cache_keeps_every_value() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=None, ttl=60)
    loads = _Loads()

    for key in [str(n) for n in range(100)]:
        await cache.get_or_load(key, loads.of(key))

    assert cache.currsize == 100
    assert cache.maxsize is None


async def test_a_zero_size_cache_keeps_nothing_and_answers_every_load() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=0, ttl=60)
    loads = _Loads()

    assert await cache.get_or_load("a", loads.of("a")) == "value-a"
    assert await cache.get_or_load("a", loads.of("a")) == "value-a"
    assert loads.keys == ["a", "a"]
    assert cache.currsize == 0


async def test_invalidate_evict_and_clear_drop_the_values_they_name() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=8, ttl=60)
    loads = _Loads()
    for key in ["a", "dir/b", "dir/c", "d"]:
        await cache.get_or_load(key, loads.of(key))

    cache.invalidate("a")
    assert cache.currsize == 3
    cache.evict(lambda key: key.startswith("dir/"))
    assert cache.currsize == 1
    cache.clear()
    assert cache.currsize == 0

    await cache.get_or_load("d", loads.of("d"))
    assert loads.keys == ["a", "dir/b", "dir/c", "d", "d"]


@pytest.mark.parametrize(
    "drop",
    [
        pytest.param(lambda cache: cache.invalidate("a"), id="invalidate"),
        pytest.param(lambda cache: cache.evict(lambda key: key == "a"), id="evict"),
        pytest.param(lambda cache: cache.clear(), id="clear"),
    ],
)
async def test_a_load_in_flight_across_a_drop_answers_its_callers_and_keeps_nothing(drop) -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=8, ttl=60)
    release = asyncio.Event()
    reads: list[str] = []

    async def stale() -> str:
        reads.append("stale")
        await release.wait()
        return "stale"

    before = asyncio.create_task(cache.get_or_load("a", stale))
    await asyncio.sleep(0)
    drop(cache)
    after = asyncio.create_task(cache.get_or_load("a", _Loads().of("a")))
    release.set()

    assert await before == "stale"
    assert await after == "value-a"
    assert await cache.get_or_load("a", _Loads().of("a")) == "value-a"
    assert reads == ["stale"]


async def test_evict_drops_every_match_and_stops_their_loads_when_a_match_expires_before_its_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(async_cache, "TTLCache", lambda maxsize, ttl: TTLCache(maxsize, ttl, timer=lambda: now[0]))
    cache: AsyncCache[str, str] = AsyncCache(maxsize=8, ttl=10)
    loads = _Loads()
    await cache.get_or_load("a", loads.of("a"))
    now[0] = 5.0
    await cache.get_or_load("b", loads.of("b"))
    release = asyncio.Event()

    async def in_flight() -> str:
        await release.wait()
        return "in-flight"

    joined = asyncio.create_task(cache.get_or_load("c", in_flight))
    await asyncio.sleep(0)

    def matches(key: str) -> bool:
        # The clock moves while the cache is walked: "a" expires after it was listed, before its drop.
        now[0] = 12.0
        return True

    cache.evict(matches)

    assert cache.currsize == 0
    assert await cache.get_or_load("b", loads.of("b")) == "value-b"
    assert await cache.get_or_load("c", loads.of("c")) == "value-c"
    release.set()
    assert await joined == "in-flight"
    assert loads.keys == ["a", "b", "b", "c"]


async def test_the_load_sees_the_callers_context_and_its_own_sets_stay_inside_it() -> None:
    cache: AsyncCache[str, str] = AsyncCache(maxsize=8, ttl=60)

    async def load() -> str:
        seen = _caller_scope.get()
        _caller_scope.set("set-by-load")
        return seen

    _caller_scope.set("caller")
    assert await cache.get_or_load("a", load) == "caller"
    assert _caller_scope.get() == "caller"


async def test_tuple_keys_cache_each_combination_apart() -> None:
    cache: AsyncCache[tuple[str, int], str] = AsyncCache(maxsize=8, ttl=60)
    loads = _Loads()

    await cache.get_or_load(("u1", 1), loads.of("u1@1"))
    await cache.get_or_load(("u1", 2), loads.of("u1@2"))
    await cache.get_or_load(("u1", 1), loads.of("u1@1"))

    assert loads.keys == ["u1@1", "u1@2"]


def test_callers_on_two_loops_in_flight_together_each_get_the_value() -> None:
    cache: AsyncCache[tuple[str, int], str] = AsyncCache(maxsize=8, ttl=60)
    first_started = threading.Event()
    second_called = threading.Event()
    both_landed = threading.Barrier(2, timeout=10)
    loads: list[str] = []

    async def load() -> str:
        loads.append("u1")
        first_started.set()
        # Held off the loop thread until the second loop has called, so that call lands mid-flight.
        assert await asyncio.to_thread(second_called.wait, 10)
        return "value"

    async def ask(*, signal: threading.Event | None) -> tuple[str, str]:
        if signal is not None:
            signal.set()
        landed = await cache.get_or_load(("u1", 0), load)
        await asyncio.to_thread(both_landed.wait)
        return landed, await cache.get_or_load(("u1", 0), load)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(asyncio.run, ask(signal=None))
        assert first_started.wait(10)
        second = pool.submit(asyncio.run, ask(signal=second_called))

        assert second.result(timeout=20) == ("value", "value")
        assert first.result(timeout=20) == ("value", "value")
    assert loads == ["u1", "u1"]
    assert cache.currsize == 1
