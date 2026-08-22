"""Network capability of the direct/host ``sandbox-local`` provider.

A bare host process runs on the host network and cannot be confined, so the direct/host
provider accepts EXACTLY network ``egress`` and REJECTS ``none`` / ``internal`` LOUDLY — it
cannot enforce network isolation on the host, and never silently falls back to an open
network for a request that asked for lockdown (PLAN_9).

The operator egress CEILING is ``egress`` (open) on this stack, so a tighter ``none`` /
``internal`` request passes the kit ceiling check and reaches the provider, where the
capability rejection bites with a typed ``SandboxSpecRejectedError`` at the create seam."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


async def test_egress_network_is_accepted_by_the_direct_provider(
    sandbox_local_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    async with sandbox_local_stack.mcp() as mcp:
        created = (
            await mcp.call_tool(
                "e2e_sandbox_probe", {"op": "create", "workspace_key": uniq("egr"), "network": "egress"}
            )
        ).data
        assert created["workspace_path"], f"an egress create must be accepted: {created}"
        await mcp.call_tool("e2e_sandbox_probe", {"op": "destroy", "session_id": created["session_id"]})


@pytest.mark.parametrize("network", ["none", "internal"])
async def test_confined_network_is_rejected_loudly(
    sandbox_local_stack: TaiStack, uniq: Callable[[str], str], network: str
) -> None:
    # A ``none`` / ``internal`` request is tighter than the open egress ceiling, so it clears
    # the kit ceiling and reaches the provider — which refuses it: a bare host process cannot
    # be confined to a locked-down network, and it never degrades to an open one instead.
    async with sandbox_local_stack.mcp() as mcp:
        rejected = await mcp.call_tool(
            "e2e_sandbox_probe",
            {"op": "create", "workspace_key": uniq("egr"), "network": network},
            raise_on_error=False,
        )
    assert rejected.is_error, f"network {network!r} must be rejected by the host provider: {rejected.data}"
    text = " ".join(getattr(part, "text", "") for part in rejected.content)
    assert "cannot enforce" in text, f"the rejection is not the loud capability mismatch: {text}"
    assert "network" in text, f"the rejection does not name the network facet: {text}"
