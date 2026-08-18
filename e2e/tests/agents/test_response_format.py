"""``response_format`` title requirements + structured-output stream suppression.

Two behaviors of the tools_agent structured-output path over the real stack:

* An untitled ``response_format`` is refused loudly. The top-level ``"title"`` is the
  structured-output name the run forces; a dict schema lacking one — or a ``oneOf``
  variant lacking a non-empty one — raises a ``ValueError`` surfaced to the caller
  (an ``is_error`` MCP result), BEFORE any model round-trip.
* A structured-output run's user-visible SSE stream carries NO synthetic
  structured-output tool-call/result frame. Routing the ``response_format`` through the
  tool-calling strategy makes the model emit a synthetic tool call carrying the payload;
  that call and its echo are internal mechanics, so neither surfaces as a
  ``tool_call_step``/``tool_result_step`` — the payload arrives exactly once as the
  terminal ``structured_final``.
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
            {"user_message": "answer the question", "response_format": untitled},
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
            {"user_message": "answer the question", "response_format": oneof_untitled},
            raise_on_error=False,
        )
        assert result2.is_error, f"an untitled oneOf variant must be refused: {result2.data}"
        assert "title" in _error_text(result2).lower(), _error_text(result2)

    # The rejection fires ahead of the run, so the scripted stub is never dialed.
    assert llm_stub.requests == [], f"a refused response_format must not reach the model: {llm_stub.requests}"


async def _run_sse(stack: TaiStack, path: str, body: dict) -> list[dict]:
    """POST an agent run over the SSE run door and return the decoded ``data:`` frames
    (each frame's JSON), draining the stream to completion."""
    url = f"http://{stack.host}:{stack.port_a}{path}"
    frames: list[dict] = []
    async with httpx.AsyncClient(timeout=15.0) as client, client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(json.loads(line[len("data:") :].strip()))
    return frames


async def test_structured_output_stream_suppresses_synthetic_tool_frames(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """A structured-output run's SSE stream carries NO synthetic structured-output
    tool-call/result frame — the payload arrives exactly once as the terminal
    ``structured_final``. The model answers through the tool-calling strategy by
    emitting a tool call named for the schema title; that call and its echo are
    suppressed, so neither a ``tool_call_step`` nor a ``tool_result_step`` surfaces."""
    value = len(uniq("v")) + 7  # a deterministic integer payload
    schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
    llm_stub.reset()
    # The structured-output tool the strategy binds is named for the schema title; the
    # model answers by calling it with the structured payload (one round-trip, then the
    # graph ends on the structured response).
    llm_stub.script([{"tool_call": {"name": "Answer", "arguments": {"value": value}}}])

    frames = await _run_sse(
        agents_stack,
        "/api/agents/tools_agent/runs",
        {"user_message": "answer with the value", "response_format": schema},
    )
    types = [frame.get("type") for frame in frames]
    # The synthetic structured-output tool call/result never surface as user-visible steps.
    assert "tool_call_step" not in types, f"the synthetic structured tool call leaked into the stream: {frames}"
    assert "tool_result_step" not in types, f"the synthetic structured tool result leaked into the stream: {frames}"
    # The structured payload arrives exactly once, as the terminal structured final.
    finals = [frame for frame in frames if frame.get("type") == "structured_final"]
    assert len(finals) == 1, f"expected exactly one structured_final: {frames}"
    assert finals[0]["data"] == {"value": value}, finals
    assert types[-1] == "stream.end", f"the stream did not terminate cleanly: {frames}"
    # Exactly one model round-trip: the single scripted structured tool call.
    assert len(llm_stub.requests) == 1, f"expected 1 LLM round-trip, saw {len(llm_stub.requests)}"
