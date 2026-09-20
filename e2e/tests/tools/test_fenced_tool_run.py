"""The run-time tier fence end to end: a ``fenced`` tool is admin-only to RUN, on every
door.

``e2e_fenced_probe`` declares the ``fenced`` tier. On the access-control-ON projection
profile a non-admin (under-scoped) principal is refused running it at the MCP edge and at
the HTTP run-tool door, while the admin principal runs it — and the SAME non-admin freely
runs a NON-fenced probe (``e2e_echo``) over the same MCP edge, so the refusal is the tier
fence, not a blanket door denial. This is the composed path a real caller's traffic takes
(channel/agent/HTTP → tool dispatch), not the fence's own seam.
"""

from __future__ import annotations

import json
from typing import Any

from tai42_e2e.stack import TaiStack

_FENCED = "e2e_fenced_probe"
_UNFENCED = "e2e_echo"


def _error_text(result: Any) -> str:
    return json.dumps([getattr(block, "text", "") for block in (result.content or [])]) + str(result)


async def test_fenced_tool_is_admin_only_over_mcp(
    projection_authz_stack: tuple[TaiStack, str, str],
) -> None:
    stack, root_token, limited_token = projection_authz_stack

    # Non-admin over MCP: AuthzMiddleware passes a RAW tool through (it gates projected
    # operations, not raw tools), so the tier fence is what refuses the fenced tool — a
    # ToolError naming the tool and the tier. The SAME principal runs a NON-fenced probe
    # over the same edge, proving the refusal is the tier, not a blanket MCP denial.
    async with stack.mcp(auth=limited_token) as mcp:
        denied = await mcp.call_tool(_FENCED, {}, raise_on_error=False)
        echoed = await mcp.call_tool(_UNFENCED, {"payload": "hi"}, raise_on_error=False)
    assert denied.is_error, f"a non-admin must be refused the fenced tool over MCP: {_error_text(denied)}"
    text = _error_text(denied).lower()
    assert _FENCED in text, f"the MCP refusal must name the tool: {_error_text(denied)}"
    assert "fenced" in text, f"the MCP refusal must name the tier: {_error_text(denied)}"
    assert not echoed.is_error, f"the same non-admin must still run a NON-fenced tool: {_error_text(echoed)}"

    # Admin over MCP: the fence admits the privileged principal — the fenced tool runs.
    async with stack.mcp(auth=root_token) as mcp:
        allowed = await mcp.call_tool(_FENCED, {"payload": "run"}, raise_on_error=False)
    assert not allowed.is_error, f"the admin must be admitted the fenced tool: {_error_text(allowed)}"
    assert allowed.data == "run", allowed.data


async def test_fenced_tool_over_http_run_tool(
    projection_authz_stack: tuple[TaiStack, str, str],
) -> None:
    stack, root_token, limited_token = projection_authz_stack
    admin = stack.api().with_token(root_token)
    non_admin = stack.api().with_token(limited_token)

    # Admin: the run-tool door executes the fenced tool.
    ok = await admin.request_raw("POST", "/api/run-tool", json={"tool_name": _FENCED, "arguments": {"payload": "x"}})
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"] == "x", ok.text

    # Non-admin: refused (the run-tool route is admin-fenced; a non-admin never reaches a
    # tool through it).
    denied = await non_admin.request_raw("POST", "/api/run-tool", json={"tool_name": _FENCED, "arguments": {}})
    assert denied.status_code == 403, denied.text
