"""``response_format`` over the native structured-output plan, end to end.

The mock leg runs provider ``openai`` / ``gpt-4o-mini``, which declares native structured
output, so a tools_agent ``response_format`` run takes the native plan:

* An untitled ``response_format`` is refused loudly BEFORE any model round-trip — the
  top-level ``"title"`` is the structured-output name, and a dict schema (or a ``oneOf``
  variant) lacking a non-empty one surfaces a ``ValueError`` as an ``is_error`` MCP result.
* A native structured run binds the schema as ``response_format`` with NO forced
  ``tool_choice``; the SSE stream carries no ``tool_call_step``/``tool_result_step`` and no
  ``message_delta`` (the JSON payload is not streamed), just one terminal ``structured_final``.
* A well-formed but schema-violating payload is re-prompted in-node up to the cap, then the
  run ends on the typed ``structured_output_unresolved_final`` outcome.
* With a stub that refuses a forced ``tool_choice``, the structured run still succeeds —
  proof the native plan sends no forced choice.
"""

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
    pytest.mark.needs("helper:llm", "setting:agent:tools_agent"),
]


def _error_text(result) -> str:
    """The text of an ``is_error`` MCP tool result (a ``CallToolResult``)."""
    return next((getattr(part, "text", "") for part in result.content), "")


async def test_untitled_response_format_is_rejected(agents_stack: TaiStack, llm_stub: LlmStub) -> None:
    """A ``response_format`` with no top-level ``"title"`` — and a ``oneOf`` whose one
    variant lacks a non-empty ``"title"`` — is refused loudly with the title-naming
    error, before any model round-trip is made."""
    llm_stub.reset()

    async with agents_stack.mcp() as mcp:
        # (a) a dict schema with no top-level title.
        untitled = {"type": "object", "properties": {"value": {"type": "integer"}}}
        result = await mcp.call_tool(
            "tools_agent",
            {"user_message": {"content": "answer the question"}, "response_format": untitled},
            raise_on_error=False,
        )
        assert result.is_error, f"an untitled response_format must be refused: {result.data}"
        assert "title" in _error_text(result).lower(), _error_text(result)

        # (b) a oneOf whose second variant carries no title (each variant binds its own
        # structured-output name, so an untitled one is refused the same way).
        oneof_untitled = {
            "title": "Top",
            "oneOf": [{"title": "Alpha", "type": "object"}, {"type": "object"}],
        }
        result2 = await mcp.call_tool(
            "tools_agent",
            {"user_message": {"content": "answer the question"}, "response_format": oneof_untitled},
            raise_on_error=False,
        )
        assert result2.is_error, f"an untitled oneOf variant must be refused: {result2.data}"
        assert "title" in _error_text(result2).lower(), _error_text(result2)

    # The rejection fires ahead of the run, so the scripted stub is never dialed.
    assert llm_stub.requests == [], f"a refused response_format must not reach the model: {llm_stub.requests}"


async def _run_sse(stack: TaiStack, path: str, body: dict) -> list[dict]:
    """POST an agent run over the SSE run door and return the decoded ``data:`` frames
    (each frame's JSON), draining the stream to completion."""
    url = f"{stack.origin(stack.port_a)}{path}"
    frames: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as client, client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(json.loads(line[len("data:") :].strip()))
    return frames


async def test_native_structured_run_sends_schema_not_forced_choice_and_streams_one_final(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """A native structured run binds the schema as ``response_format`` with no forced
    ``tool_choice``; the SSE stream carries no synthetic tool frames and no ``message_delta``
    (the JSON payload is not streamed), just one terminal ``structured_final``."""
    value = len(uniq("v")) + 7  # a deterministic integer payload
    schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
    llm_stub.reset()
    # Under the native plan the model answers with the JSON payload as its message content.
    llm_stub.script([{"content": json.dumps({"value": value})}])

    frames = await _run_sse(
        agents_stack,
        "/api/agents/tools_agent/runs",
        {"user_message": {"content": "answer with the value"}, "response_format": schema},
    )
    types = [frame.get("type") for frame in frames]
    assert "tool_call_step" not in types, f"a tool frame leaked into the native stream: {frames}"
    assert "tool_result_step" not in types, f"a tool-result frame leaked into the native stream: {frames}"
    assert "message_delta" not in types, f"the native plan must not stream the JSON payload: {frames}"
    finals = [frame for frame in frames if frame.get("type") == "structured_final"]
    assert len(finals) == 1, f"expected exactly one structured_final: {frames}"
    assert finals[0]["data"] == {"value": value}, finals
    assert types[-1] == "stream.end", f"the stream did not terminate cleanly: {frames}"
    assert len(llm_stub.requests) == 1, f"expected 1 LLM round-trip, saw {len(llm_stub.requests)}"
    # The request carried the portable schema as response_format and bound NO forced tool choice.
    request = llm_stub.requests[0]
    sent_schema = request["response_format"]["json_schema"]["schema"]
    assert sent_schema["properties"]["value"]["type"] == "integer", request
    assert request.get("tool_choice") in (None, "auto", "none"), request


@pytest.mark.needs("setting:TAI_AGENTS_STRUCTURED_OUTPUT_REPROMPT_CAP=3")
async def test_native_reprompt_cap_yields_the_typed_outcome(agents_stack: TaiStack, llm_stub: LlmStub) -> None:
    """A native run whose well-formed JSON never conforms (an int64-oversized integer) is
    re-prompted in-node up to the per-run cap, then ends with the typed, non-fatal
    ``structured_output_unresolved_final`` outcome — exactly cap + 1 = 4 round-trips, the second
    request carrying the re-prompt as a user message."""
    schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    llm_stub.reset()
    oversized = json.dumps({"value": 9223372036854775808})  # one past the platform int64 ceiling
    llm_stub.script([{"content": oversized} for _ in range(4)])

    frames = await _run_sse(
        agents_stack,
        "/api/agents/tools_agent/runs",
        {"user_message": {"content": "answer with a number"}, "response_format": schema},
    )
    types = [frame.get("type") for frame in frames]
    outcome = [frame for frame in frames if frame.get("type") == "structured_output_unresolved_final"]
    assert len(outcome) == 1, f"expected the typed re-prompt-cap outcome: {frames}"
    assert outcome[0]["attempts"] == 4, outcome
    assert "stream.error" not in types, f"the capped run must not surface a generic failure: {frames}"
    assert types[-1] == "stream.end", f"the stream did not terminate cleanly: {frames}"
    assert len(llm_stub.requests) == 4, f"expected 4 LLM round-trips, saw {len(llm_stub.requests)}"
    # The second request carries the re-prompt fed back as a user message.
    second = llm_stub.requests[1]
    assert any(message.get("role") == "user" for message in second["messages"][1:]), second


async def test_native_structured_run_sends_no_forced_tool_choice(agents_stack: TaiStack, llm_stub: LlmStub) -> None:
    """With the stub refusing any forced ``tool_choice``, a native structured run still
    succeeds — proof the native plan sends no forced choice (a forced path would 400)."""
    schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
    llm_stub.reset()
    llm_stub.refuse_forced_tool_choice()
    llm_stub.script([{"content": json.dumps({"value": 11})}])

    frames = await _run_sse(
        agents_stack,
        "/api/agents/tools_agent/runs",
        {"user_message": {"content": "answer with the value"}, "response_format": schema},
    )
    finals = [frame for frame in frames if frame.get("type") == "structured_final"]
    assert len(finals) == 1, f"the refusing stub rejected the run — a forced tool_choice was sent: {frames}"
    assert finals[0]["data"] == {"value": 11}, finals
