"""The kit session lifecycle driven THROUGH the running SUT via ``e2e_sandbox_probe``.

The probe acquires a session through the ONE acquisition chokepoint
(``require_sandbox``) and drives create / exec / put / get / touch / info / list /
reap / destroy against the process-based fake provider — the process-through-the-real-SUT
counterpart to the in-process kit conformance suite. The fake keeps its sessions in a
process-level registry, so this rides the single-worker ``sandbox_stack`` where every
probe call lands in the same server process.

Durability is the property the deep-agent durable suite leans on: a ``persistent``
workspace SURVIVES its session's reap (a later create on the same ``workspace_key``
re-attaches the bytes), while an ``ephemeral`` one dies with the session. Backend-invariant,
so the module is ``backendless``.
"""

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


# ---- the full lifecycle --------------------------------------------------


async def test_session_lifecycle_round_trips(sandbox_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    key = uniq("life")
    async with sandbox_stack.mcp() as mcp:
        created = await _probe(mcp, "create", workspace_key=key, durability="ephemeral")
        session_id = created["session_id"]
        workspace_path = created["workspace_path"]
        assert session_id, created
        assert workspace_path, created

        # exec runs real code in the session and returns the ExecResult triple.
        token = uniq("tok")
        run = await _probe(mcp, "exec", session_id=session_id, argv=["python", "-c", f"print({token!r})"])
        assert run["exit_code"] == 0, run
        assert token in run["stdout"], run
        assert run["stderr"] == "", run

        # put/get round-trips bytes through the workspace.
        await _probe(mcp, "put", session_id=session_id, path="note.txt", data="carried")
        got = await _probe(mcp, "get", session_id=session_id, path="note.txt")
        assert got["data"] == "carried", got

        # touch is the keep-alive turn; it succeeds against a live session.
        touched = await _probe(mcp, "touch", session_id=session_id)
        assert touched["touched"] is True, touched

        # info() round-trips the requested spec fields and reports the SAME workspace root
        # the session exposes.
        info = await _probe(mcp, "info", session_id=session_id)
        assert info["session_id"] == session_id, info
        assert info["workspace_path"] == workspace_path, info
        assert info["durability"] == "ephemeral", info

        # list() names the live session.
        listing = await _probe(mcp, "list")
        assert session_id in listing["sessions"], listing

        # destroy tears it down; a later op against the id is a loud miss.
        destroyed = await _probe(mcp, "destroy", session_id=session_id)
        assert destroyed["destroyed"] is True, destroyed
        gone = await mcp.call_tool("e2e_sandbox_probe", {"op": "info", "session_id": session_id}, raise_on_error=False)
        assert gone.is_error, gone


async def test_info_round_trips_durability_for_a_persistent_session(
    sandbox_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    key = uniq("persist-info")
    async with sandbox_stack.mcp() as mcp:
        created = await _probe(mcp, "create", workspace_key=key, durability="persistent")
        info = await _probe(mcp, "info", session_id=created["session_id"])
        # The persistent tier is round-tripped off the ledger, distinguishing it from the
        # ephemeral case above.
        assert info["durability"] == "persistent", info
        assert info["workspace_path"] == created["workspace_path"], info
        await _probe(mcp, "destroy", session_id=created["session_id"])


# ---- durability: persistent survives a reap, ephemeral dies --------------


async def test_persistent_workspace_survives_a_reap(sandbox_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    key = uniq("survive")
    async with sandbox_stack.mcp() as mcp:
        created = await _probe(mcp, "create", workspace_key=key, durability="persistent", ttl_seconds=1)
        await _probe(mcp, "put", session_id=created["session_id"], path="kept.txt", data="durable-bytes")

        # Poll reap until the short TTL lapses and the session is torn down — the session is
        # reaped but the persistent workspace is PRESERVED (reap never removes a durable one).
        async def reaped_the_session() -> bool:
            outcome = await _probe(mcp, "reap")
            return created["session_id"] in outcome["reaped"]

        await wait_for_async(reaped_the_session, deadline=10.0, message="the short-TTL session was never reaped")

        # A new session on the SAME key re-attaches the durable workspace and reads the bytes.
        reattached = await _probe(mcp, "create", workspace_key=key, durability="persistent", ttl_seconds=60)
        got = await _probe(mcp, "get", session_id=reattached["session_id"], path="kept.txt")
        assert got["data"] == "durable-bytes", got
        await _probe(mcp, "destroy", session_id=reattached["session_id"])


async def test_ephemeral_workspace_does_not_survive_its_session(
    sandbox_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    key = uniq("ephemeral")
    async with sandbox_stack.mcp() as mcp:
        created = await _probe(mcp, "create", workspace_key=key, durability="ephemeral")
        await _probe(mcp, "put", session_id=created["session_id"], path="scratch.txt", data="transient")
        await _probe(mcp, "destroy", session_id=created["session_id"])

        # A new ephemeral session on the same key gets a FRESH scratch workspace — the prior
        # bytes are gone, so the read is a loud miss.
        reborn = await _probe(mcp, "create", workspace_key=key, durability="ephemeral")
        miss = await mcp.call_tool(
            "e2e_sandbox_probe",
            {"op": "get", "session_id": reborn["session_id"], "path": "scratch.txt"},
            raise_on_error=False,
        )
        assert miss.is_error, miss
        text = " ".join(getattr(part, "text", "") for part in miss.content)
        assert "miss" in text, text
        await _probe(mcp, "destroy", session_id=reborn["session_id"])
