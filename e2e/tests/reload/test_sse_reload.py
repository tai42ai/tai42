"""A reload driven by a tool call over the SSE transport answers without waiting the drain budget.

``tai serve --transport sse`` runs every call of a session under the session's ``GET /sse``
stream, which carries the call's reply and ends only when its client disconnects. The
stream is never counted against the retire's drain budget, and the reload records the
session's call as its driver, so the reload answers once its rebuild and fleet wait are
done — long before a drain budget pinned far above them.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from tai42_e2e.manifests import build_replicas_stack
from tai42_e2e.topology import StackConfig, StackResources, Topology
from tai42_e2e.variants import Variants

pytestmark = pytest.mark.needs("process", "kind:backend", "setting:transport:sse", "setting:api_tools", "store:redis")

# A drain budget far above a rebuild plus the fleet wait: a reload that waited it out on the
# session's own stream answers only after it.
_DRAIN_BUDGET_S = 90.0


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


# The MCP reload call may run to the harness read bound (150 s), so the per-test bound sits
# above it: the stack boot inside the test (120) + the reload call's read bound (150) + the
# listing before it (15).
@pytest.mark.timeout(300)
async def test_a_reload_over_the_sse_transport_answers_without_waiting_the_drain_budget(fresh_stack) -> None:
    stack = fresh_stack(
        _build_sse_stack, env_overrides={"TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS": str(int(_DRAIN_BUDGET_S))}
    )
    async with stack.mcp() as mcp:
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
