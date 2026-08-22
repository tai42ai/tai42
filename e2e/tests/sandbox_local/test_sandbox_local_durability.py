"""Host-directory durability of the direct/host ``sandbox-local`` provider.

Durability under the direct provider IS the named host directory
``<SANDBOX_LOCAL_ROOT>/<workspace_key>`` (PLAN_9): a ``persistent`` session keyed by
``workspace_key`` writes under that stable dir. The two teardown paths are pinned as a
CONTRAST — a ``reap`` (``remove_workspace=False``) PRESERVES the named host dir, so a
LATER ``create_session`` on the SAME key RE-ATTACHES the very same dir and reads the
bytes back; an explicit ``destroy`` (``remove_workspace=True``) REMOVES even the durable
named dir, so a re-create lands on a freshly remade dir with none of the prior bytes.
An ``ephemeral`` session's workspace is a throwaway scratch dir that never survives its
own teardown at all."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.mcp import McpClient
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.backendless


async def _probe(mcp: McpClient, op: str, **kwargs) -> dict:
    """Drive one probe op, raising loudly on a tool error (the outcome dict on success)."""
    result = await mcp.call_tool("e2e_sandbox_probe", {"op": op, **kwargs})
    return result.data


async def test_persistent_workspace_reattaches_the_named_host_dir_across_sessions(
    sandbox_local_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    workspace_key = uniq("ws")
    token = uniq("durable")

    async with sandbox_local_stack.mcp() as mcp:
        first = await _probe(mcp, "create", workspace_key=workspace_key, durability="persistent", ttl_seconds=1)
        first_path = first["workspace_path"]
        # SANDBOX_LOCAL_ROOT is honored as a named host dir keyed by the workspace key.
        assert first_path.endswith(f"/{workspace_key}"), f"the persistent workspace is not the named host dir: {first}"

        await _probe(mcp, "put", session_id=first["session_id"], path="state.txt", data=token)

        # End the first session WITHOUT an explicit destroy: poll reap until the short TTL
        # lapses and the session is torn down. A reap is remove_workspace=False, so the named
        # host dir is PRESERVED (persistence means surviving reap / session-end, NOT an
        # explicit destroy).
        async def reaped_the_session() -> bool:
            outcome = await _probe(mcp, "reap")
            return first["session_id"] in outcome["reaped"]

        await wait_for_async(reaped_the_session, deadline=10.0, message="the short-TTL session was never reaped")

        # A LATER create on the SAME key re-attaches the identical named host dir...
        second = await _probe(mcp, "create", workspace_key=workspace_key, durability="persistent", ttl_seconds=60)
        assert second["workspace_path"] == first_path, (
            "a re-create on the same key did not re-attach the same host dir: "
            f"{first_path} != {second['workspace_path']}"
        )

        # ...and the bytes written by the reaped session survive into it.
        got = await _probe(mcp, "get", session_id=second["session_id"], path="state.txt")
        assert got["data"] == token, f"the durable host dir did not retain the bytes across a reap: {got}"

        await _probe(mcp, "destroy", session_id=second["session_id"])


async def test_explicit_destroy_removes_the_persistent_named_host_dir(
    sandbox_local_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    workspace_key = uniq("ws")
    token = uniq("durable")

    async with sandbox_local_stack.mcp() as mcp:
        first = await _probe(mcp, "create", workspace_key=workspace_key, durability="persistent")
        first_path = first["workspace_path"]
        await _probe(mcp, "put", session_id=first["session_id"], path="state.txt", data=token)

        # The CONTRAST to reap: an explicit destroy is remove_workspace=True, so it removes
        # even the durable named host dir (and its sidecar) — persistence does not survive an
        # explicit teardown.
        await _probe(mcp, "destroy", session_id=first["session_id"])

        # A re-create on the same key remakes the identical named dir path, but freshly empty.
        second = await _probe(mcp, "create", workspace_key=workspace_key, durability="persistent")
        assert second["workspace_path"] == first_path, (
            f"a re-create on the same key did not remake the same named host dir: {second['workspace_path']}"
        )

        # The prior bytes are gone — the explicit destroy removed the durable host dir.
        miss = await mcp.call_tool(
            "e2e_sandbox_probe",
            {"op": "get", "session_id": second["session_id"], "path": "state.txt"},
            raise_on_error=False,
        )
        assert miss.is_error, (
            f"an explicit destroy must remove the durable host dir: the read should miss, got {miss.data}"
        )

        await _probe(mcp, "destroy", session_id=second["session_id"])


async def test_ephemeral_workspace_does_not_survive_destroy(
    sandbox_local_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    workspace_key = uniq("eph")
    token = uniq("scratch")

    async with sandbox_local_stack.mcp() as mcp:
        first = await _probe(mcp, "create", workspace_key=workspace_key, durability="ephemeral")
        await _probe(mcp, "put", session_id=first["session_id"], path="state.txt", data=token)
        await _probe(mcp, "destroy", session_id=first["session_id"])

        # A fresh ephemeral session lands on a throwaway scratch dir — never the prior one.
        second = await _probe(mcp, "create", workspace_key=workspace_key, durability="ephemeral")
        assert second["workspace_path"] != first["workspace_path"], (
            f"an ephemeral re-create reused the prior scratch dir: {second['workspace_path']}"
        )

        # The prior bytes are gone — the ephemeral workspace did not survive destroy.
        miss = await mcp.call_tool(
            "e2e_sandbox_probe",
            {"op": "get", "session_id": second["session_id"], "path": "state.txt"},
            raise_on_error=False,
        )
        assert miss.is_error, f"ephemeral scratch must not survive: the read should miss, got {miss.data}"

        await _probe(mcp, "destroy", session_id=second["session_id"])
