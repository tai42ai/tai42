"""A fleet reload driven by a tool call over a sub-MCP mount answers on the mount's session.

The mount ``/app/<slug>`` serves ``reload_config`` (a projected operation the replicas
manifest exposes as a tool). The call rebuilds the worker's serving generation, whose rebuild
drops every cached mount; the mount's session is the transport carrying the call's reply, so
it stays open until the call has answered with the fleet report, and only then is retired with
the previous generation: the next call on the same handle re-initialises a fresh session.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.needs(
    "store:redis", "setting:api_tools", "mutable", "topology:replicas", "setting:SUB_MCP_REDIS_URL"
)


# The MCP reload call may run to the harness read bound (150 s), so the per-test bound sits
# above it: the stack boot inside the first test's timer (120) + the reload call's read
# bound (150) + a session re-init (15) + the delete's reload-gate wait (10).
@pytest.mark.timeout(300)
async def test_reload_over_a_mount_answers_with_the_fleet_report(
    replicas_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    slug = uniq("mountreload").replace("_", "-")
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    await api_a.post("/api/sub-mcp", json={"slug": slug, "tools": ["reload_config"]}, retry_on_reloading=True)
    try:
        async with replicas_stack.mcp(port=replicas_stack.port_a, path=f"/app/{slug}") as mcp:
            # Prime the SDK's per-session tool-output-schema cache so the post-call result
            # validation issues no follow-up tools/list on the session the reload retires.
            assert await mcp.tool_names() == ["reload_config"]
            session_before = mcp.session_id
            result = await mcp.call_tool("reload_config", {}, retry_on_reloading=True)
            # The call answered on the mount session it was made on.
            assert mcp.session_id == session_before
            data = result.data if isinstance(result.data, dict) else result.structured_content
            assert isinstance(data, dict), f"reload_config returned no result map: {result!r}"
            assert data["op"] == "reload_config"
            assert data["reachable"] is True, f"the bus was unreachable: {data}"
            outcomes = {row["name"]: row["outcome"] for row in data["results"]}
            census = {worker.name for worker in replicas_stack.census()}
            assert census <= outcomes.keys(), f"not every census worker was reported: {census} vs {outcomes}"
            assert all(outcomes[name] == "applied" for name in census), f"a worker did not apply: {data}"
            # The reload retired the mount session once it had answered: the next call on the
            # same handle re-initialises a fresh session on the new generation's mount.
            assert await mcp.tool_names() == ["reload_config"]
            assert mcp.session_id is not None
            assert mcp.session_id != session_before
    finally:

        async def _deleted() -> bool:
            response = await api_a.request_raw("DELETE", f"/api/sub-mcp/{slug}")
            if response.status_code == 503:  # the reload gate is still held: retriable
                return False
            assert response.status_code == 200, f"DELETE /api/sub-mcp/{slug} -> {response.status_code}"
            return True

        await wait_for_async(_deleted, deadline=10.0, message=f"sub-MCP {slug} delete never left the reload gate")
