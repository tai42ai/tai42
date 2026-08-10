"""``mcp_tools_agent`` over the stack: tools loaded from an MCP configuration.

The agent opens a ``fastmcp`` client against a caller-supplied MCP server, turns
its tools into LangChain tools, and runs the generic tools-agent loop over them.
The MCP source here is the stack's OWN ``/mcp`` endpoint (the existing tool seam —
no external server needed): the agent, running inside a worker, connects back to
its own server, discovers the probe tools, and the scripted stub drives the
tool -> result -> final loop over them."""

from __future__ import annotations

import json
from collections.abc import Callable

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


async def test_mcp_tools_agent_runs_tools_from_mcp_config_over_mcp(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    echoed = uniq("mcp_echo")
    final = f"done {uniq('run')}"
    llm_stub.reset()
    llm_stub.script(
        [
            {"tool_call": {"name": "e2e_echo", "arguments": {"payload": echoed}}},
            {"content": final},
        ]
    )

    # The MCP source is the stack's own streamable-http /mcp endpoint; the agent
    # loads its tools (the probe tools) from there.
    mcp_config = {"mcpServers": {"local": {"url": f"http://{agents_stack.host}:{agents_stack.port_a}/mcp"}}}

    async with agents_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "mcp_tools_agent",
            {"mcp_config": mcp_config, "user_message": "echo the payload back"},
        )

    assert final in json.dumps(result.data), f"mcp_tools_agent did not return the scripted final: {result.data}"
    requests = llm_stub.requests
    assert len(requests) == 2, f"expected 2 LLM round-trips, saw {len(requests)}"

    # The tools the agent LOADED over MCP were bound to the model: without a working
    # mcp_config load the first completion would advertise no e2e_echo function.
    advertised = {tool["function"]["name"] for tool in requests[0].get("tools", [])}
    assert "e2e_echo" in advertised, f"the MCP-loaded tool was never bound to the model: {sorted(advertised)}"

    # The tool was EXECUTED over MCP and its result fed back: the echoed token exists
    # nowhere in the prompt (the user message never carries it) — it can only reach
    # the second completion as the tool message the MCP call produced.
    tool_messages = [m for m in requests[1]["messages"] if m.get("role") == "tool"]
    assert tool_messages, f"the second completion carried no tool result: {requests[1]['messages']}"
    assert echoed in json.dumps(tool_messages), "the MCP tool's result never reached the model"
