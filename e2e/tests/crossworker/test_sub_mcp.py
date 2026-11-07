"""A sub-MCP registered on replica A must serve on replica B, via the
durable sub-MCP store + dispatch-time fallback (the SUB_MCP_REDIS_URL the
replicas profile sets). B never saw the registration."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs("probe-tools", "mutable", "topology:replicas", "setting:SUB_MCP_REDIS_URL")


async def _serves(stack: TaiStack, port: int, slug: str) -> bool:
    try:
        async with stack.mcp(port=port, path=f"/app/{slug}") as mcp:
            return "e2e_echo" in await mcp.tool_names()
    except Exception:
        return False


# The module's only test boots its stack (120) + the register (10 gate poll + 60) + B's serve
# poll (5) and call (15) + A's whole-fleet reload (60) + A's serve read (15) + B's serve poll (5)
# + the delete poll (10) + the two listings (2 x 15) + A's second reload (60) + its poll (5).
# Each reload over REST measured 20-42 s under load.
@pytest.mark.timeout(400)
async def test_sub_mcp_registered_on_a_serves_on_b(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    slug = uniq("submcp").replace("_", "-")
    api_a = replicas_stack.api(port=replicas_stack.port_a)

    await api_a.post("/api/sub-mcp", json={"slug": slug, "tools": ["e2e_echo"]})

    # B never saw the registration; the store fallback builds it on demand.
    await wait_for_async(
        lambda: _serves(replicas_stack, replicas_stack.port_b, slug),
        deadline=5.0,
        message="sub-MCP registered on A never served on B",
    )
    async with replicas_stack.mcp(port=replicas_stack.port_b, path=f"/app/{slug}") as mcp:
        result = await mcp.call_tool("e2e_echo", {"payload": "via-b"})
    assert result.data == "via-b"

    # Survives a reload on A (the reset() wipe half). A reloads in-line
    # (synchronous before the POST returns) and rehydrates the slug from the store;
    # B reloads only once A's fleet reload_config fan-out reaches it, so B re-serves
    # after a short, eventually-consistent delay — poll rather than sample once.
    await api_a.post("/api/config/env", json={"env": {}})
    assert await _serves(replicas_stack, replicas_stack.port_a, slug)
    await wait_for_async(
        lambda: _serves(replicas_stack, replicas_stack.port_b, slug),
        deadline=5.0,
        message="B stopped serving the slug after A's fleet reload",
    )

    # Delete on B: the store-backed list drops the slug on both immediately. A's fleet reload
    # may still hold B's reload gate (sub-MCP requests are not gated, so B serves before its
    # gate frees); the client polls past the retriable refusal within the delete's budget.
    api_b = replicas_stack.api(port=replicas_stack.port_b)
    await api_b.delete(f"/api/sub-mcp/{slug}", ready_deadline=10.0)
    listing_a = await api_a.get("/api/sub-mcp")
    listing_b = await api_b.get("/api/sub-mcp")
    assert slug not in listing_a
    assert slug not in listing_b

    # A already BUILT the slug, so it may keep serving until its next reload.
    await api_a.post("/api/config/env", json={"env": {}})
    await wait_for_async(
        lambda: _not_serving(replicas_stack, replicas_stack.port_a, slug),
        deadline=5.0,
        message="A kept serving the deleted slug after its reload",
    )


async def _not_serving(stack: TaiStack, port: int, slug: str) -> bool:
    return not await _serves(stack, port, slug)
