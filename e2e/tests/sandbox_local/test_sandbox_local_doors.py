"""The sandbox-kind doors against the REAL direct/host ``sandbox-local`` provider.

The §3a door doctrine, but over ``build_sandbox_local_stack`` (the REAL PLAN_9
provider — host-subprocess exec, host-dir workspaces, no docker) instead of the
process-based fake: the ``GET /api/sandbox`` identity door reports the real provider
present alongside the resolved security-as-config policy, and the ``e2e_sandbox_probe``
consumer door drives a real host-subprocess lifecycle (create/exec/put/get/list/destroy)
returning the contract ``ExecResult``. The direct provider needs no container image."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.stack import TaiStack

# The sandbox-local stack runs no backend worker — the sandbox surface is
# backend-invariant, so skip this module on non-default backend legs.
pytestmark = pytest.mark.backendless


async def test_sandbox_door_reports_local_provider_and_resolved_policy(sandbox_local_stack: TaiStack) -> None:
    info = await sandbox_local_stack.api().get("/api/sandbox")

    # The real direct/host provider is registered and named on the identity door.
    assert info["present"] is True, f"the sandbox-local provider must report present: {info}"
    assert info["module"] == "tai42_sandbox_local.provider", f"unexpected provider module: {info}"
    assert info["provider"] == "LocalSandbox", f"unexpected provider class: {info}"

    # The resolved security-as-config policy sits at the floor the host provider can
    # satisfy — isolation ``none`` and egress ``egress`` — pinned by the stack env.
    assert info["policy"] == {
        "egress": "egress",
        "isolation": "none",
        "scrub_transcript": False,
        "durable": True,
    }, f"unexpected resolved policy: {info['policy']}"


async def test_sandbox_probe_drives_real_host_subprocess_lifecycle(
    sandbox_local_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    token = uniq("host")
    workspace_key = uniq("ws")

    async with sandbox_local_stack.mcp() as mcp:
        created = (await mcp.call_tool("e2e_sandbox_probe", {"op": "create", "workspace_key": workspace_key})).data
        session_id = created["session_id"]
        assert created["workspace_path"], f"the created session carried no host workspace path: {created}"

        # A real host subprocess: the ``exec`` runs on the host and returns the ExecResult.
        exec_result = (
            await mcp.call_tool(
                "e2e_sandbox_probe",
                {"op": "exec", "session_id": session_id, "argv": ["sh", "-c", f"echo {token}"]},
            )
        ).data
        assert exec_result["exit_code"] == 0, f"the host exec failed: {exec_result}"
        assert token in exec_result["stdout"], f"the host exec did not echo the token: {exec_result}"

        # File transfer round-trips through the host-dir workspace.
        await mcp.call_tool(
            "e2e_sandbox_probe", {"op": "put", "session_id": session_id, "path": "probe.txt", "data": token}
        )
        got = (
            await mcp.call_tool("e2e_sandbox_probe", {"op": "get", "session_id": session_id, "path": "probe.txt"})
        ).data
        assert got["data"] == token, f"the host-dir round-trip lost the bytes: {got}"

        listed = (await mcp.call_tool("e2e_sandbox_probe", {"op": "list"})).data
        assert session_id in listed["sessions"], f"the live session is not listed: {listed}"

        destroyed = (await mcp.call_tool("e2e_sandbox_probe", {"op": "destroy", "session_id": session_id})).data
        assert destroyed["destroyed"] is True, f"destroy did not confirm: {destroyed}"
