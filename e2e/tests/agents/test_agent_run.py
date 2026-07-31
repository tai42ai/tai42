"""C7 — the cross-process LLM->tool->LLM loop. The agent runs inside the real
server process and talks to a real socket LLM (the scripted stub); the whole
loop (tool call, tool result fed back, final content) happens over HTTP."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import TaiStack

# The agents stack runs no backend worker; skip this module on non-default
# backend legs (they exercise no backend seam).
pytestmark = pytest.mark.backendless


async def test_tools_agent_llm_tool_llm_loop_over_http(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    echoed = uniq("echo")
    final = f"done {uniq('run')}"
    llm_stub.reset()
    llm_stub.script(
        [
            {"tool_call": {"name": "e2e_echo", "arguments": {"payload": echoed}}},
            {"content": final},
        ]
    )

    async with agents_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "tools_agent", {"user_message": "echo the payload back", "tool_names": ["e2e_echo"]}
        )

    assert final in json.dumps(result.data), f"agent did not return the scripted final content: {result.data}"
    requests = llm_stub.requests
    assert len(requests) == 2, f"expected 2 LLM round-trips, saw {len(requests)}"
    # The echoed token is nowhere in the prompt, so it can only reach the second
    # completion as the tool message the real tool call produced.
    tool_messages = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages, f"the second completion carried no tool result: {requests[1]['messages']}"
    assert echoed in json.dumps(tool_messages), "the tool result never reached the model"


async def test_authored_agent_preset_created_on_one_worker_runs_via_http(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    name = uniq("authored")
    final = f"authored {uniq('run')}"
    llm_stub.reset()
    llm_stub.script([{"content": final}])

    api = agents_stack.api()
    await api.post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "tools_agent",
            "description": "authored agent preset",
            "fixed_kwargs": {"user_message": "hi"},
        },
    )

    # Run the authored agent over its SSE run route; the last frame is the final.
    frames = await _run_sse(agents_stack, f"/api/agents/authored/{name}/runs", {})
    assert any(final in frame for frame in frames), f"authored agent SSE never delivered the scripted final: {frames}"


async def _run_sse(stack: TaiStack, path: str, body: dict) -> list[str]:
    url = f"http://{stack.host}:{stack.port_a}{path}"
    frames: list[str] = []
    async with httpx.AsyncClient(timeout=15.0) as client, client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(line[len("data:") :].strip())
    return frames
