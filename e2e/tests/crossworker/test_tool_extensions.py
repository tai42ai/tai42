"""C4 / G3 — a tool-extension applied on replica A. The manifest gains the mapping and
A serves the branch locally; the apply's ``reload_config`` fan-out reaches every worker
over the bus, so the backend runtime AND the sibling HTTP replica rebind the branch too.
The test verifies that fan-out rebind and the ApplyResult report shape."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack


async def test_tool_extension_apply_visible_on_sibling(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)

    # Apply the extension on A, polling past A's boot-time reload gate.
    result = await api_a.post("/api/tools/e2e_echo/extensions", json={"combos": [["batch"]]}, retry_on_reloading=True)

    # A serves the branch locally (the local apply).
    async def a_serves_batch() -> bool:
        async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
            return "e2e_echo_batch" in await mcp.tool_names()

    await wait_for_async(a_serves_batch, deadline=5.0, message="A never served e2e_echo_batch after local apply")

    # The apply fans out over the worker bus: the response embeds the mode-wrapped
    # fleet report, and at least one sibling confirmed the rebind reload.
    fanout = result.get("fanout")
    assert isinstance(fanout, dict), f"tool-extension apply carried no fanout report: {result}"
    assert fanout["mode"] == "fleet", f"tool-extension apply did not fan out over the fleet: {fanout}"
    assert any(r["outcome"] == "applied" for r in fanout["results"]), f"no worker confirmed the rebind: {fanout}"

    # The backend worker rebound the branch and can run it.
    async with replicas_stack.mcp() as mcp:
        await mcp.call_tool(
            "run_tool_sync_task",
            {"tool_name": "e2e_echo_batch", "arguments": {"params": []}},
            retry_on_reloading=True,
        )

    # The sibling HTTP replica rebound from the same fan-out, so it serves the branch too.
    async def b_serves_batch() -> bool:
        async with replicas_stack.mcp(port=replicas_stack.port_b) as mcp:
            return "e2e_echo_batch" in await mcp.tool_names()

    await wait_for_async(b_serves_batch, deadline=5.0, message="B never served e2e_echo_batch from the fan-out")
