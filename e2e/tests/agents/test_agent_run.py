"""The cross-process LLM->tool->LLM loop. The agent runs inside the real
server process and talks to a real socket LLM (the scripted stub); the whole
loop (tool call, tool result fed back, final content) happens over HTTP."""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# The agents stack runs no backend worker; skip this module on non-default
# backend legs (they exercise no backend seam).
pytestmark = [
    pytest.mark.backendless,
    # The scripted llm_stub round-trips (script + assert on llm_stub.requests) are the LLM
    # MOCK leg; a real-provider leg is exercised on the e2e creds host, not in CI,
    # so the stub-bound module steps aside when the 'llm' seam is real. Inert in the default
    # mock run — is_real("llm") is False, so collection is byte-for-byte today's.
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
]


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
            "tools_agent", {"user_message": {"content": "echo the payload back"}, "tool_names": ["e2e_echo"]}
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
            "fixed_kwargs": {"user_message": {"content": "hi"}},
        },
    )

    # Run the authored agent over its SSE run route; the last frame is the final.
    frames = await _run_sse(agents_stack, f"/api/agents/authored/{name}/runs", {})
    assert any(final in frame for frame in frames), f"authored agent SSE never delivered the scripted final: {frames}"


async def test_agent_preset_baking_a_system_message_runs_on_both_doors(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    # A preset over ``tools_agent`` that bakes a per-call ``system_message`` is a fastmcp
    # transformed tool: it fills every omitted optional argument with its schema default
    # before forwarding, so ``system_prompt`` arrives as an explicit ``None``. The run tool
    # treats that null-default ``None`` as unset, so the guard refusing ``system_prompt``
    # beside ``system_message`` never fires — the preset runs over BOTH doors it can enter.
    name = uniq("sysmsg")
    final = f"sysmsg {uniq('run')}"
    llm_stub.reset()
    llm_stub.script([{"content": final}, {"content": final}])

    await agents_stack.api().post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "tools_agent",
            "description": "agent preset baking a per-call system message",
            "fixed_kwargs": {"system_message": {"content": "answer tersely"}},
        },
    )

    # Run-tool HTTP door.
    http_result = await agents_stack.api().post(
        "/api/run-tool",
        json={"tool_name": name, "arguments": {"user_message": {"content": "hi"}}},
    )
    assert final in json.dumps(http_result), f"run-tool HTTP door did not return the scripted final: {http_result}"

    # MCP tools/call edge.
    async with agents_stack.mcp() as mcp:
        mcp_result = await mcp.call_tool(name, {"user_message": {"content": "hi"}})
    assert final in json.dumps(mcp_result.data), f"MCP edge did not return the scripted final: {mcp_result.data}"


async def _run_sse(stack: TaiStack, path: str, body: dict) -> list[str]:
    url = f"http://{stack.host}:{stack.port_a}{path}"
    frames: list[str] = []
    async with httpx.AsyncClient(timeout=15.0) as client, client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(line[len("data:") :].strip())
    return frames
