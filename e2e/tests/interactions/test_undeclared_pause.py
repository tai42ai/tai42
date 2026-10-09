"""A tool that parks without declaring it can pause is refused by name on every door.

``e2e_undeclared_park`` is registered WITHOUT ``tai42/pauses`` and returns the
``SuspendedInteraction`` of a real async ``ask``. The platform refuses that park signal loudly,
naming the tool, wherever it is recognised:

* the synchronous run-tool door (``POST /api/run-tool``) answers 501;
* the MCP ``tools/call`` edge answers a tool error;
* a ``tools_agent`` run whose scripted model calls ``e2e_undeclared_park_chain`` (a pausing
  ``chain`` branch whose body dispatches the undeclared tool through ``run_tool``) ends failed:
  the refusal leaves the run raw, so the model never receives a tool-error message for the call
  and never gets a second turn to retry it.

Each leg reads the fixture's own record to prove the ``ask`` fired exactly once — the refusal
happens after the body ran, and nothing re-runs it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host",
    ),
    pytest.mark.needs(
        "kind:interactions",
        "probe-tools",
        "helper:llm",
        "store:redis",
        "topology:replicas",
        "setting:agent:tools_agent",
        "setting:checkpoint:redis",
        "setting:extension:chain",
    ),
]

_UNDECLARED = "e2e_undeclared_park"
_REFUSAL = f"tool {_UNDECLARED!r} returned a park signal but does not declare that it can pause"
# Far enough out that no expiry reaper resolves the refused park while a leg reads it.
_EXPIRY_SECONDS = 3600.0


def _ask_fired_once(stack: TaiStack, question: str) -> None:
    fired = stack.records(f"undeclared_park:{question}")
    assert len(fired) == 1, f"the undeclared tool's ask did not fire exactly once: {fired}"


async def test_run_tool_door_refuses_an_undeclared_park(
    agent_async_park_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    question = uniq("question")
    body: Any = await agent_async_park_stack.api(port=agent_async_park_stack.port_a).post(
        "/api/run-tool",
        json={"tool_name": _UNDECLARED, "arguments": {"question": question, "expiry_seconds": _EXPIRY_SECONDS}},
        expect=501,
        retry_on_reloading=True,
    )
    assert _REFUSAL in json.dumps(body), f"the run-tool door's refusal did not name the tool: {body}"
    _ask_fired_once(agent_async_park_stack, question)


async def test_mcp_edge_refuses_an_undeclared_park(
    agent_async_park_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    question = uniq("question")
    async with agent_async_park_stack.mcp(port=agent_async_park_stack.port_a) as mcp:
        result = await mcp.call_tool(
            _UNDECLARED,
            {"question": question, "expiry_seconds": _EXPIRY_SECONDS},
            raise_on_error=False,
            retry_on_reloading=True,
        )
    assert result.is_error, f"the MCP edge returned an undeclared park: {result.data}"
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert _REFUSAL in text, f"the MCP edge's refusal did not name the tool: {text}"
    _ask_fired_once(agent_async_park_stack, question)


async def test_agent_run_fails_on_an_undeclared_park_inside_a_pausing_branch(
    agent_async_park_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    question = uniq("question")
    branch = f"{_UNDECLARED}_chain"
    llm_stub.reset()
    llm_stub.script(
        [
            {
                "tool_call": {
                    "name": branch,
                    "arguments": {
                        "question": question,
                        "expiry_seconds": _EXPIRY_SECONDS,
                        "jq_expression": {"content": "{}"},
                        "next_tool_name": "e2e_echo",
                    },
                }
            },
            # Served only if the refusal were turned into a model-visible tool error.
            {"content": "retried after a tool error"},
        ]
    )
    async with agent_async_park_stack.mcp(port=agent_async_park_stack.port_a) as mcp:
        result = await mcp.call_tool(
            "tools_agent",
            {
                "user_message": {"content": question},
                "tool_names": [branch],
                "langgraph_config": {"configurable": {"thread_id": uniq("thread")}},
            },
            raise_on_error=False,
            retry_on_reloading=True,
        )
    tool_messages = [
        message
        for request in llm_stub.requests
        for message in request.get("messages", [])
        if message.get("role") == "tool"
    ]
    assert tool_messages == [], f"the model received a tool message for the refused call: {tool_messages}"
    assert len(llm_stub.requests) == 1, f"the model got another turn after the refusal: {len(llm_stub.requests)}"
    assert result.is_error, f"the agent run did not fail on the undeclared park: {result.data}"
    text = " ".join(getattr(part, "text", "") for part in result.content)
    assert _REFUSAL in text, f"the agent run's failure did not name the tool: {text}"
    _ask_fired_once(agent_async_park_stack, question)
