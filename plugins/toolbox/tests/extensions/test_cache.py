"""Tests for the ``cache`` tool extension."""

import asyncio
import inspect

from tai42_contract.extensions import ExtensionKind

import tai42_toolbox.extensions.cache as cache_module
from tai42_toolbox._internal.extensions import cache_store
from tai42_toolbox.extensions.cache import cache


def _tool(text: str) -> str:
    return text


def test_registers_as_wrapper_named_cache(capture_registration):
    # No body-locality requirement: the process-local store costs cross-process
    # hits under a relocating extension but never a wrong result.
    assert capture_registration(cache_module) == [("cache", ExtensionKind.WRAPPER, False)]


def test_wraps_a_tool_ending_in_var_keyword():
    # The exp control kwarg must be inserted before a trailing **kwargs so
    # signature construction stays valid for such tools.
    def tool(text: str, **kwargs: object) -> str:
        return text

    composed = inspect.signature(cache(tool, "tool", "desc"))
    names = list(composed.parameters)

    assert names[-1] == "kwargs"
    assert composed.parameters["exp"].kind is inspect.Parameter.KEYWORD_ONLY


def test_reserved_params_value():
    assert cache.reserved_params == frozenset({"exp"})


def test_composed_signature_is_original_plus_exp():
    original = inspect.signature(_tool)
    composed = inspect.signature(cache(_tool, "tool", "desc"))

    original_names = set(original.parameters)
    composed_names = set(composed.parameters)

    assert original_names <= composed_names
    assert composed_names - original_names == {"exp"}
    assert composed.parameters["exp"].kind is inspect.Parameter.KEYWORD_ONLY
    # The wrapped tool's own parameter is presented unchanged.
    assert composed.parameters["text"].annotation is str


def test_hit_miss_and_expiry(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(cache_store.time, "monotonic", lambda: clock["now"])

    calls = 0

    async def backing(text: str) -> str:
        nonlocal calls
        calls += 1
        return text.upper()

    cached = cache(backing, "backing", "desc")

    async def scenario() -> None:
        nonlocal calls
        # Miss: computes and stores with a 10s TTL (expire at 1010).
        assert await cached("hi", exp=10) == "HI"
        assert calls == 1

        # Hit within the TTL: no recompute.
        clock["now"] = 1005.0
        assert await cached("hi", exp=10) == "HI"
        assert calls == 1

        # Past the TTL: the entry expired, so it recomputes.
        clock["now"] = 1020.0
        assert await cached("hi", exp=10) == "HI"
        assert calls == 2

    asyncio.run(scenario())


def test_single_flight_runs_backing_once():
    calls = 0

    async def scenario() -> None:
        nonlocal calls
        entered = asyncio.Event()
        release = asyncio.Event()

        async def backing(text: str) -> str:
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return text.upper()

        cached = cache(backing, "backing", "desc")

        first = asyncio.create_task(cached("hi"))
        # Wait until the first caller is inside the backing call (holding the key lock).
        await entered.wait()
        second = asyncio.create_task(cached("hi"))
        # Let the second caller reach and park on the held key lock.
        await asyncio.sleep(0)

        release.set()
        results = await asyncio.gather(first, second)

        assert results == ["HI", "HI"]

    asyncio.run(scenario())
    # The second caller read the freshly written value instead of re-running.
    assert calls == 1


def test_cached_none_is_a_hit_not_a_miss():
    # A stored ``None`` must read back as a hit, not be mistaken for a miss (the
    # MISS sentinel is distinct from any real value), so the backing runs once.
    calls = 0

    async def backing(text: str) -> None:
        nonlocal calls
        calls += 1
        return None

    cached = cache(backing, "backing", "desc")

    async def scenario() -> None:
        assert await cached("hi") is None
        assert await cached("hi") is None

    asyncio.run(scenario())
    assert calls == 1
