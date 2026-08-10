"""C7 — the real ``tai42-monitoring-langfuse`` plugin against the compose-provided
self-hosted Langfuse. Opt-in: collects only with ``TAI_E2E_MONITORING=1`` (the
compose ``monitoring`` profile up); skipped at collection otherwise."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
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
