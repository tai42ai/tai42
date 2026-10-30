"""A background agent run whose tool call reloads the config completes with its real outcome.

The agent runs as a background tool run (a supervised run) inside the server process and
talks to the scripted LLM stub. Its tool node runs each tool call in a child task of the
run; the scripted model calls ``reload_config``, which rebuilds the serving generation the
run was admitted on. The retire of that generation leaves the run that drives it running,
so the reload's fleet report reaches the model and the run records ``succeeded``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs under TAI_E2E_REAL",
    ),
    pytest.mark.needs("helper:llm", "setting:agent:tools_agent", "kind:tool_runs", "mutable"),
]


# The per-test bound covers the stack boot inside the first test's timer (120) + the wait for
# the run to end (the boot bound for its boot-sized rebuild, plus 60 for its two model turns).
@pytest.mark.timeout(300)
async def test_a_background_agent_run_whose_tool_call_reloads_the_config_succeeds(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    final = f"reloaded {uniq('run')}"
    llm_stub.reset()
    llm_stub.script([{"tool_call": {"name": "reload_config", "arguments": {}}}, {"content": final}])

    api = agents_stack.api()
    submitted = await api.post(
        "/api/tool-runs",
        json={
            "tool_name": "tools_agent",
            "arguments": {"user_message": {"content": "reload the config"}, "tool_names": ["reload_config"]},
        },
        expect=202,
        retry_on_reloading=True,
    )
    run_id = submitted["run_id"]

    async def _terminal() -> dict[str, Any] | None:
        view = await api.get(f"/api/tool-runs/{run_id}", retry_on_reloading=True)
        return view if view["status"] not in ("pending", "running") else None

    view = await wait_for_async(
        _terminal, deadline=agents_stack.infra.settings.boot_timeout + 60.0, message=f"agent run {run_id} never ended"
    )
    assert view["status"] == "succeeded", f"the agent run did not complete: {view}"
    assert final in json.dumps(view["result"]), f"the run did not return the model's final answer: {view}"
    # The reload answered the tool call with its fleet report, which reached the model.
    requests = llm_stub.requests
    assert len(requests) == 2, f"expected 2 LLM round-trips, saw {len(requests)}"
    tool_messages = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages, f"the second completion carried no tool result: {requests[1]['messages']}"
    assert "reload_config" in json.dumps(tool_messages), f"the fleet report never reached the model: {tool_messages}"
