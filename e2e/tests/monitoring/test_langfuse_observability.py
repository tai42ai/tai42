"""C7 — the real ``tai42-monitoring-langfuse`` plugin against the compose-provided
self-hosted Langfuse. Opt-in: collects only with ``TAI_E2E_MONITORING=1`` (the
compose ``monitoring`` profile up); skipped at collection otherwise."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import TaiStack

# The monitoring stack runs no backend worker; skip this module on non-default
# backend legs (they exercise no backend seam).
pytestmark = pytest.mark.backendless


async def test_tool_run_spans_reach_langfuse_and_serve_back(
    monitoring_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    payload = uniq("trace")
    async with monitoring_stack.mcp() as mcp:
        # e2e_echo_monitor is the monitor-wrapped branch: each standalone call opens
        # one SpanKind.TOOL trace, which the langfuse backend records — the "run" the
        # observability reader then serves back. A plain e2e_echo call is untraced.
        for _ in range(3):
            await mcp.call_tool("e2e_echo_monitor", {"payload": payload})

    api = monitoring_stack.api()

    # READ side: the observability routes source exclusively from the registered
    # monitoring backend's reader and answer empty under the no-op default, so
    # non-zero data proves the Langfuse reader end to end. Ingestion is async
    # through the Langfuse worker, hence a generous deadline.
    async def reader_serves_data() -> bool:
        runs = await api.get("/api/observability/runs")
        items = runs.get("items") if isinstance(runs, dict) else None
        return bool(items)

    await wait_for_async(
        reader_serves_data,
        deadline=60.0,
        message="observability reader never served langfuse-sourced runs",
    )

    # The list row is a SUMMARY: status / tokens / latency are first-class, and no
    # trace body (observations/spans) nor per-observation model rides on the row.
    runs = await api.get("/api/observability/runs")
    row = runs["items"][0]
    assert row["status"] in ("success", "error")
    assert "totalTokens" in row
    assert "latencyMs" in row
    assert "model" not in row
    assert "observations" not in row
    assert "spans" not in row

    # DETAIL leg: the body door (get_trace) still serves the full trace with its
    # observations — the list summary and the detail are distinct read paths.
    detail = await api.get(f"/api/observability/runs/{row['traceId']}/trace")
    assert detail["traceId"] == row["traceId"]
    assert "spans" in detail


async def test_agent_run_trace_reaches_langfuse_and_serves_back(
    monitoring_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    marker = uniq("agentrun")
    llm_stub.reset()
    # One content frame ends the agent's LLM->tool->LLM loop immediately (no tool
    # call); the whole run is traced natively by the agents plugin's monitoring
    # callbacks, so the marker carried in the user message rides into the trace input.
    llm_stub.script([{"content": f"done {marker}"}])

    async with monitoring_stack.mcp() as mcp:
        await mcp.call_tool("tools_agent", {"user_message": f"trace the marker {marker}"})

    api = monitoring_stack.api()

    # READ side: the observability run list exposes each trace's input via
    # ``inputPreview`` (a server-bounded preview string — the short user message is
    # kept intact). The run-list HTTP filter has no name/input clause, so match the marker
    # client-side over the served items: its presence proves THIS agent run's trace
    # reached Langfuse and is served back through the platform read path only.
    async def marker_run_served() -> bool:
        runs = await api.get("/api/observability/runs")
        items = runs.get("items") if isinstance(runs, dict) else None
        return bool(items) and marker in json.dumps(items)

    await wait_for_async(
        marker_run_served,
        deadline=60.0,
        message="observability reader never served the marked agent run",
    )
