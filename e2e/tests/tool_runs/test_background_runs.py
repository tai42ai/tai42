"""C7 (P1) — a background tool-run started on replica A reaches its terminal
state observable on replica B (run state lives in Redis; per-worker supervisors
are an implementation detail the test must not see)."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.stack import TaiStack


async def test_tool_run_started_on_a_polls_terminal_on_b(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    api_b = replicas_stack.api(port=replicas_stack.port_b)

    submitted = await api_a.post(
        "/api/tool-runs", json={"tool_name": "e2e_sleep", "arguments": {"seconds": 1}}, expect=202
    )
    run_id = submitted["run_id"]

    async def terminal_on_b() -> bool:
        view = await api_b.get(f"/api/tool-runs/{run_id}")
        return view["status"] == "succeeded"

    await wait_for_async(terminal_on_b, deadline=10.0, message="tool run never reached succeeded on B")
