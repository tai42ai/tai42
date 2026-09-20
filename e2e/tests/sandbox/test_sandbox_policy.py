"""Security-as-config ENFORCED at the SINGLE kit session-create chokepoint.

The kit base owns the enforcement, so the fake provider inherits it and the SAME operator
policy bites IDENTICALLY through every consumer door — it is applied ONCE at the kit create
seam, not per door. This module drives the two doors reachable on ``build_sandbox_stack``
(the ``e2e_sandbox_probe`` consumer door and the ``sandbox_exec`` base tool) against a policy
tightened at boot via the ``TAI_MCP_SANDBOX_*`` knobs, and asserts each violation surfaces as
a LOUD typed rejection — never a silent widening or downgrade. The isolation FLOOR is proven
by inheritance: a spec that requests no isolation resolves to the operator floor and is
accepted, rather than clamped.

Backend-invariant, so ``backendless``. The knobs are recycle-class, so the policy is set at
boot through one ``fresh_stack`` env override shared by every leg below.

REACH NOTE: the ``sandbox_exec`` and ``e2e_sandbox_probe`` doors do not expose an isolation
request or consumer labels, so the isolation-BELOW-floor and reserved-namespace-label
rejections are not drivable through them here (the base tool deliberately leaves isolation
unset to inherit the floor); those two are covered by the in-process kit conformance suite.
"""

from __future__ import annotations

import pytest

from tai42_e2e.manifests import build_sandbox_stack
from tai42_e2e.mcp import McpClient
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless

_IMAGE = "img@sha256:" + "0" * 64


def _tight_policy_stack(res, variants):
    """``build_sandbox_stack`` with the egress ceiling closed, durable disabled, and the
    isolation floor raised to ``vm`` — the one boot every policy leg here reads against."""
    config = build_sandbox_stack(res, variants)
    config.env["TAI_MCP_SANDBOX_EGRESS"] = "none"
    config.env["TAI_MCP_SANDBOX_DURABLE"] = "false"
    config.env["TAI_MCP_SANDBOX_ISOLATION"] = "vm"
    return config


async def _probe_error(mcp: McpClient, **kwargs) -> str:
    result = await mcp.call_tool("e2e_sandbox_probe", {"op": "create", **kwargs}, raise_on_error=False)
    assert result.is_error, result
    return " ".join(getattr(part, "text", "") for part in result.content)


async def _exec_error(mcp: McpClient, **kwargs) -> str:
    result = await mcp.call_tool(
        "sandbox_exec", {"argv": ["python", "-c", "pass"], "image": _IMAGE, **kwargs}, raise_on_error=False
    )
    assert result.is_error, result
    return " ".join(getattr(part, "text", "") for part in result.content)


async def test_policy_chokepoint_rejects_loudly_on_every_reachable_door(fresh_stack) -> None:
    stack: TaiStack = fresh_stack(_tight_policy_stack)
    async with stack.mcp() as mcp:
        # NETWORK CEILING: the egress ceiling is closed to ``none``; a consumer requesting the
        # looser ``egress`` is refused at the shared create seam through EITHER door — never a
        # silent widening.
        assert "looser than the egress ceiling" in await _probe_error(mcp, network="egress")
        assert "looser than the egress ceiling" in await _exec_error(mcp, network="egress")

        # DURABLE GATE: durable workspaces are disabled; a persistent request is refused (no
        # silent ephemeral downgrade) through EITHER door. The network is left at the closed
        # ceiling so the durable gate is the sole reason for the refusal.
        assert "durable workspaces are disabled" in await _probe_error(mcp, durability="persistent", network="none")
        assert "durable workspaces are disabled" in await _exec_error(mcp, durability="persistent", network="none")

        # ISOLATION FLOOR (inheritance): a spec that requests no isolation resolves to the
        # operator floor (here raised to ``vm``) and is ACCEPTED — the floor is inherited, not a
        # reason to reject a request that asked for nothing below it.
        created = await mcp.call_tool(
            "e2e_sandbox_probe", {"op": "create", "workspace_key": "policy-floor", "network": "none"}
        )
        session_id = created.data["session_id"]
        assert session_id, created.data
        await mcp.call_tool("e2e_sandbox_probe", {"op": "destroy", "session_id": session_id})
