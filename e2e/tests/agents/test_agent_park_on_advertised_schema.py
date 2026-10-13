"""A park through a tool whose advertised output schema is the author's own, end to end over MCP.

A preset declaring an ``output_schema`` over the ``tools_agent`` run tool advertises that schema on
the MCP ``tools/list``; the run tool can pause, so the MCP edge advertises the park markers beside
it. The scripted model calls the ``e2e_agent_async_ask`` probe tool, the run parks, and the MCP
client — which validates ``structuredContent`` against the advertised schema — reads the park
marker with ``isError: false``. An answer through the other replica resumes the parked run.

Runs over ``agent_async_park_stack`` (replicas, redis checkpoint, auth off).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from tai42_contract.interactions import RESUME_BUFFERED_MARKER_SCHEMA, SUSPENDED_INTERACTION_MARKER_SCHEMA
from tai42_kit.interactions.park_marker import read_suspended_interaction_marker

from tai42_e2e import wait_for_async
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
        "probe-tools",
        "helper:llm",
        "store:redis",
        "topology:replicas",
        "setting:agent:tools_agent",
        "setting:checkpoint:redis",
    ),
]

_ANSWER_SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}
_TOOL_NAMES = ["e2e_agent_async_ask", "e2e_record_identity"]


async def test_a_preset_with_an_output_schema_over_a_parking_agent_parks_over_mcp_and_resumes(
    agent_async_park_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    stack = agent_async_park_stack
    preset = uniq("answer_preset")
    thread_id = uniq("thread")
    question = uniq("question")
    await stack.api().post(
        "/api/presets",
        json={
            "name": preset,
            "base_tool": "tools_agent",
            "description": "A tools agent answering in a fixed shape.",
            "output_schema": _ANSWER_SCHEMA,
        },
        retry_on_reloading=True,
    )

    llm_stub.reset()
    llm_stub.script(
        [
            {"tool_call": {"name": "e2e_agent_async_ask", "arguments": {"question": question, "expiry_seconds": 3600}}},
            {"tool_call": {"name": "e2e_record_identity", "arguments": {"thread_id": thread_id}}},
            {"content": json.dumps({"answer": "resumed"})},
        ]
    )

    async with stack.mcp(port=stack.port_a) as mcp:

        async def _advertised() -> dict | None:
            listed = {tool.name: tool for tool in await mcp.list_tools()}
            return listed[preset].outputSchema if preset in listed else None

        advertised = await wait_for_async(_advertised, deadline=20.0, message="the preset never reached replica A")
        # The MCP edge advertises the author's schema AND the park markers for a tool that can pause.
        markers = [SUSPENDED_INTERACTION_MARKER_SCHEMA, RESUME_BUFFERED_MARKER_SCHEMA]
        assert advertised == {"anyOf": [_ANSWER_SCHEMA, *markers]}

        result = await mcp.call_tool(
            preset,
            {
                "user_message": {"content": question},
                "tool_names": _TOOL_NAMES,
                "langgraph_config": {"configurable": {"thread_id": thread_id}},
            },
            raise_on_error=False,
            retry_on_reloading=True,
        )

    # The client validated the park against the advertised schema and reads it as a park, not an error.
    assert result.is_error is False, result.content
    marker = read_suspended_interaction_marker(result.structured_content)
    assert marker is not None, f"the preset run did not park: {result.structured_content}"
    interaction_id = marker["interaction_id"]

    answered = await stack.api(port=stack.port_b).post(
        f"/api/interactions/{interaction_id}/answer", json={"answer": "resume-me"}
    )
    assert answered["status"] == "answered"

    async def _resumed() -> bool | None:
        return bool(stack.records(f"agent_resume:{thread_id}")) or None

    await wait_for_async(_resumed, deadline=20.0, message="the answered preset park never resumed its drive")

    # Let the resume drive consume its whole script before the module's next leg resets the stub.
    async def _script_consumed() -> bool | None:
        return (len(llm_stub.requests) >= 3) or None

    await wait_for_async(_script_consumed, deadline=20.0, message="the resume drive never finished its script")
