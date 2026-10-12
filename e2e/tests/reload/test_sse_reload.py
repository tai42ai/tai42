"""A reload driven over the SSE transport answers without waiting the drain budget, then ends the old sessions.

``tai serve --transport sse`` runs every call of a session under the session's ``GET /sse``
stream, which carries the call's reply. The stream is never counted against the retire's
drain budget, and the reload records the session's call as its driver, so the reload answers
once its rebuild and fleet wait are done — long before a drain budget pinned far above them.
The retired generation then ends every session it served: the SSE transport has no
session-termination message, so a session opened before the reload reads its closed
connection on its next call, and a new connection lists the tools.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import anyio
import pytest
from mcp.shared.exceptions import McpError

from tai42_e2e.manifests import build_replicas_stack
from tai42_e2e.topology import StackConfig, StackResources, Topology
from tai42_e2e.variants import Variants

pytestmark = pytest.mark.needs(
    "process",
    "kind:backend",
    "setting:transport:sse",
    "setting:api_tools",
    "store:redis",
    "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
    "setting:TAI_BUS_APPLY_TIMEOUT=330",
)

# A drain budget far above a rebuild plus the fleet wait: a reload that waited it out on the
# session's own stream answers only after it.
_DRAIN_BUDGET_S = 90.0
# The fleet apply window covers the retire's drain floor at that budget (3 x 90 s) plus the
# reload's own 60 s, as the platform default does above three default drains.
_APPLY_WINDOW_S = 330.0


def _build_sse_stack(res: StackResources, variants: Variants) -> StackConfig:
    """The replicas manifest served by one ``tai serve --transport sse`` worker (an SSE
    session is pinned to the worker that opened it) beside the backend worker."""
    return replace(
        build_replicas_stack(res, variants),
        name="sse-reload",
        topology=Topology.MULTIWORKER,
        workers=1,
        transport="sse",
        run_metrics=False,
    )


# How long the session opened before the reload may take to read its closed connection: its
# generation's retire ends it as soon as the driving call has answered.
_CLOSED_READ_BOUND_S = 30.0


def _reads_the_closed_connection(exc: BaseException) -> bool:
    """Whether ``exc`` is what the SDK's SSE client reads on a session whose stream the server
    ended: ``Connection closed`` for a call pending when it ended, an immediate
    ``ClosedResourceError`` for a call issued after."""
    return (isinstance(exc, McpError) and "Connection closed" in str(exc)) or isinstance(exc, anyio.ClosedResourceError)


# The MCP reload call may run to the harness read bound (the stack's 330 s apply window + the
# 120 s boot bound = 450 s), so the per-test bound sits above it: the stack boot inside the test
# (120) + the reload call's read bound (450) + the listings before it (2 x 15) + the old
# session's closed read (30) + a new session's listing (15).
@pytest.mark.timeout(645)
async def test_a_reload_over_the_sse_transport_answers_without_waiting_the_drain_budget(fresh_stack) -> None:
    stack = fresh_stack(
        _build_sse_stack,
        env_overrides={
            "TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": str(int(_DRAIN_BUDGET_S)),
            "TAI_BUS_APPLY_TIMEOUT": str(int(_APPLY_WINDOW_S)),
        },
    )
    async with stack.mcp() as before, stack.mcp() as mcp:
        # A second session, opened before the reload and idle through it.
        assert "reload_config" in await before.tool_names()
        assert "reload_config" in await mcp.tool_names()
        start = time.monotonic()
        result = await mcp.call_tool("reload_config", {}, retry_on_reloading=True)
        took = time.monotonic() - start
        data = result.data if isinstance(result.data, dict) else result.structured_content
        assert isinstance(data, dict), f"reload_config returned no result map: {result!r}"
        assert data["op"] == "reload_config"
        assert data["reachable"] is True, f"the bus was unreachable: {data}"
        # The worker serving the SSE session applied the reload it drove (the fleet's own outcomes are
        # the fan-out suite's subject).
        served_by = [row for row in data["results"] if row["name"].startswith("serve")]
        assert served_by, f"the serving worker was not reported: {data}"
        assert all(row["outcome"] == "applied" for row in served_by), f"the serving worker did not apply: {data}"
        assert took < _DRAIN_BUDGET_S, f"the reload answered after {took:.1f}s, waiting the drain budget out"

        # The retired generation ended the session opened before the reload: its next call
        # reads the closed connection, at once rather than never.
        with pytest.raises((McpError, anyio.ClosedResourceError)) as closed:
            async with asyncio.timeout(_CLOSED_READ_BOUND_S):
                await before.tool_names()
        assert _reads_the_closed_connection(closed.value), f"the old session read {closed.value!r}"

    # A new connection reaches the new generation.
    async with stack.mcp() as fresh:
        assert "reload_config" in await fresh.tool_names()
