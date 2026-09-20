"""The ``cache`` tool extension (WRAPPER) through the stack.

Observable effect: a memoized tool is executed once for identical calls. The
``e2e_record`` probe RPUSHes a side-effect record every time its body runs, so
branching it with ``cache`` and calling the branch twice with identical arguments
must leave exactly ONE record — the second call is served from the cache store
without re-running the body."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


async def test_second_identical_call_is_served_from_cache(
    extensions_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    key = uniq("cache")
    args = {"key": key, "value": "v"}

    async with extensions_stack.mcp() as mcp:
        first = await mcp.call_tool("e2e_record_cache", args)
        second = await mcp.call_tool("e2e_record_cache", args)

    # Both calls return the wrapped tool's result (the second from the store).
    assert "recorded" in str(first.data)
    assert "recorded" in str(second.data)

    # The body ran exactly once: only one side-effect record exists for the key.
    async def one_record() -> bool:
        return len(extensions_stack.records(key)) >= 1

    await wait_for_async(one_record, deadline=5.0, message="cached tool never recorded its single execution")
    records = extensions_stack.records(key)
    assert len(records) == 1, f"cache did not memoize: expected 1 execution record, got {records}"


async def test_different_arguments_are_a_cache_miss(extensions_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    """A cache that memoized on anything but its arguments — a constant key, a key
    ignoring the args — would serve the FIRST result for every call. So a second call
    with DIFFERENT arguments must miss: it re-runs the body and returns its own
    result, not the first one's."""
    first_key = uniq("miss_a")
    second_key = uniq("miss_b")

    async with extensions_stack.mcp() as mcp:
        first = await mcp.call_tool("e2e_record_cache", {"key": first_key, "value": "v"})
        second = await mcp.call_tool("e2e_record_cache", {"key": second_key, "value": "v"})

    # Each call ran its own body: each key has its own execution record.
    async def both_recorded() -> bool:
        return bool(extensions_stack.records(first_key)) and bool(extensions_stack.records(second_key))

    await wait_for_async(both_recorded, deadline=5.0, message="a different-argument call was served from the cache")
    # ONE execution record under EACH key is the keying proof: a cache whose key
    # ignored the arguments would have served the second call from the first call's
    # entry, and the second key's body would never have run at all.
    assert len(extensions_stack.records(first_key)) == 1
    assert len(extensions_stack.records(second_key)) == 1
    assert "recorded" in str(first.data)
    assert "recorded" in str(second.data)
