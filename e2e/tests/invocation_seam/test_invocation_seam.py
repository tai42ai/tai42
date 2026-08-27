"""The ambient invoked-tool seam over the REAL edges: ``current_tool_invocation()``
carries the invoked tool's own name for the span of a call — populated over the MCP call
edge, the in-process ``run_tool`` dispatch, AND the cross-process backend
``run_tool_sync_task`` dispatch — and reads ``None`` where no tool is executing.

The fixture ``e2e_invocation_probe`` reports both: ``inside`` (the seam read from its own
body) and ``outside`` (the seam read on a bare thread that inherits no deposit — the
out-of-call ``None``)."""

from __future__ import annotations

from typing import Any

from tai42_e2e.stack import TaiStack


def _data(result: Any) -> dict:
    data = result.data if result.data is not None else result.structured_content
    assert isinstance(data, dict), f"unexpected probe shape: {data!r}"
    return data


async def test_seam_populated_over_mcp_edge_and_none_outside(seams_stack: TaiStack) -> None:
    async with seams_stack.mcp() as mcp:
        result = await mcp.call_tool("e2e_invocation_probe", retry_on_reloading=True)
    data = _data(result)
    # The MCP call edge deposits the called tool's own name for the span of the call.
    assert data["inside"] == "e2e_invocation_probe", data
    # No deposit propagates to a bare thread — the seam reads its out-of-call None.
    assert data["outside"] is None, data


async def test_seam_populated_over_run_tool_path(seams_stack: TaiStack) -> None:
    async with seams_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "run_tool",
            {"tool_name": "e2e_invocation_probe", "arguments": {}},
            retry_on_reloading=True,
        )
    data = _data(result)
    # The in-process run_tool dispatch arms the seam for the DISPATCHED tool, so the probe
    # reads its own name (not the run_tool vehicle's) from inside its body.
    assert data["inside"] == "e2e_invocation_probe", data
    assert data["outside"] is None, data


async def test_seam_populated_over_backend_dispatch(seams_stack: TaiStack) -> None:
    async with seams_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "run_tool_sync_task",
            {"tool_name": "e2e_invocation_probe", "arguments": {}},
            retry_on_reloading=True,
        )
    data = _data(result)
    # The cross-process backend dispatch runs the probe INSIDE the backend worker and still
    # arms the seam for the DISPATCHED tool, so the probe reads its own name there.
    assert data["inside"] == "e2e_invocation_probe", data
    assert data["outside"] is None, data
