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


# The kwarg set the flows engine's agent holder passes on the call each turn. When a flow node
# holds ``tools_agent`` (or a preset over it), the engine builds the call arguments from the
# holder's baked ``tool_kwargs`` merged with the turn's message and hands that dict to the held
# tool — the system/user messages, the client tool list, the LLM provider and its model kwargs,
# and any forced structured-output schema all ride as CALL arguments, not as the preset's own
# baked kwargs. A saved preset over ``tools_agent`` receiving this set is the shape a real
# deployment runs: the preset transform fills every omitted optional (``system_prompt`` among
# them) with its schema default, so the run must accept a per-call ``system_message`` beside that
# null default.
_ENGINE_AGENT_HOLDER_PER_CALL_KWARGS: dict[str, object] = {
    "user_message": {"content": "engine holder user marker"},
    "system_message": {"content": "engine holder system marker"},
    "tool_names": ["e2e_echo"],
    "llm_provider": "openai",
    "llm_kwargs": {"temperature": 0.0},
    "response_format": {
        "title": "Answer",
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    },
}


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


async def test_agent_preset_over_agent_receives_the_engine_holder_per_call_kwargs_on_both_doors(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    # A saved authored preset over ``tools_agent`` is CALLED with the engine agent holder's full
    # per-call kwarg set (``_ENGINE_AGENT_HOLDER_PER_CALL_KWARGS``) on BOTH doors it can enter — the
    # run-tool HTTP door and the MCP edge. The engine hands these as call arguments each turn, not
    # as the preset's baked kwargs, so the preset carries only its own minimal baked config and the
    # system/user messages and run config arrive per call. The preset transform fills every omitted
    # optional (``system_prompt`` among them) with its schema default, so the run must accept the
    # per-call ``system_message`` beside that null default. ``response_format`` forces structured
    # output: the strategy binds a tool named for the schema ``title`` (``Answer``), the model
    # answers by calling it, and the run returns the parsed payload.
    name = uniq("holderkw")
    http_value = f"http-{uniq('answer')}"
    mcp_value = f"mcp-{uniq('answer')}"
    llm_stub.reset()
    llm_stub.script(
        [
            {"tool_call": {"name": "Answer", "arguments": {"value": http_value}}},
            {"tool_call": {"name": "Answer", "arguments": {"value": mcp_value}}},
        ]
    )

    # The harness's authored agent preset over ``tools_agent`` — a saved named alias with no baked
    # message/run config of its own, so the engine's per-call set is what shapes each turn.
    await agents_stack.api().post(
        "/api/presets",
        json={
            "name": name,
            "base_tool": "tools_agent",
            "description": "authored agent preset over tools_agent",
        },
    )

    # Run-tool HTTP door: the engine holder's per-call set as the call arguments.
    http_result = await agents_stack.api().post(
        "/api/run-tool",
        json={"tool_name": name, "arguments": dict(_ENGINE_AGENT_HOLDER_PER_CALL_KWARGS)},
    )
    assert http_value in json.dumps(http_result), (
        f"run-tool HTTP door did not return the structured answer: {http_result}"
    )

    # MCP tools/call edge: the same per-call set.
    async with agents_stack.mcp() as mcp:
        mcp_result = await mcp.call_tool(name, dict(_ENGINE_AGENT_HOLDER_PER_CALL_KWARGS))
    assert mcp_value in json.dumps(mcp_result.data), f"MCP edge did not return the structured answer: {mcp_result.data}"

    # The per-call ``system_message`` and ``user_message`` reached the model, so the engine holder's
    # kwarg set travelled through the preset transform intact on both doors.
    recorded = json.dumps(llm_stub.requests)
    system_marker = _ENGINE_AGENT_HOLDER_PER_CALL_KWARGS["system_message"]["content"]  # type: ignore[index]
    user_marker = _ENGINE_AGENT_HOLDER_PER_CALL_KWARGS["user_message"]["content"]  # type: ignore[index]
    assert system_marker in recorded, f"the per-call system_message never reached the model: {llm_stub.requests}"
    assert user_marker in recorded, f"the per-call user_message never reached the model: {llm_stub.requests}"


async def _run_sse(stack: TaiStack, path: str, body: dict) -> list[str]:
    url = f"http://{stack.host}:{stack.port_a}{path}"
    frames: list[str] = []
    async with httpx.AsyncClient(timeout=15.0) as client, client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(line[len("data:") :].strip())
    return frames
