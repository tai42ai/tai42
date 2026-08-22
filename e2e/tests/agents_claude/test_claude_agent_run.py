"""§3b — a full ``claude_code`` turn over the identity-less ephemeral doors.

Each leg boots a fresh stack wired to a scripted runner (the fake sandbox runs
``python -m tai_runner`` as a real subprocess, so the whole exec path — framing, the
``hello`` version gate, event streaming, the terminal ``result`` — is genuinely driven).
The turn is reached over ``/mcp`` (drained to the terminal value) and the SSE run route
(one frame per ``StreamEvent``), covering:

* a plain streamed answer -> ``MessageFinal`` (drained value + the SSE ``message_delta`` /
  ``message_final`` frames);
* a structured result carrying a top-level ``title`` (the ``response_format`` path);
* the thinking -> ``ReasoningStep`` event mapping;
* the unhonored params refused loudly (``tools`` / ``resume_checkpoint_id`` / ``llm_provider``
  are not ``ClaudeCodeInput`` fields, so the tool face and the SSE route both reject them);
* the exactly-one model-auth rule (neither / both set is a loud run-start config error, each
  auth mode accepted alone);
* the digest-only ``session_image`` rule (a bare tag is a loud run-start error).

Threaded continuity (turn-1 ``hello`` session-id capture, turn-2 ``resume``) is a
conversation-bridge property: ``claude_code`` takes a ``thread_id`` ONLY as a trusted
in-process bridge kwarg, and that door is not wired on ``build_claude_agent_stack``, so the
capture-and-resume leg is exercised in the plugin's own drive tests, not here.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest
from fastmcp.exceptions import ToolError

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

from ._claude_support import CLAUDE_RUN_PATH, claude_stack, error_text, frames_of_type, run_sse

# The stack runs no backend worker; the scripted runner is the ``claude_agent`` MOCK leg, so
# this module steps aside when that seam is real (the real turn is the §5 smoke).
pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("claude_agent"),
        reason="scripted runner stub is the 'claude_agent' mock leg; the real turn is the §5 smoke",
    ),
]


async def test_answer_drains_to_message_final_over_both_doors(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "answer")

    # Tool face: the run drains to the terminal message text (the contract terminal rule).
    async with stack.mcp() as mcp:
        result = await mcp.call_tool("claude_code", {"user_message": "hi"}, retry_on_reloading=True)
    assert result.data == "hello from the stub", result.data

    # SSE: the streamed text deltas arrive as ``message_delta`` frames and the turn ends on a
    # ``message_final`` carrying the same assembled text, then a terminal ``stream.end``.
    frames = await run_sse(stack, {"user_message": "hi"})
    deltas = frames_of_type(frames, "message_delta")
    assert "".join(f["text"] for f in deltas) == "hello from the stub", frames
    finals = frames_of_type(frames, "message_final")
    assert [f["text"] for f in finals] == ["hello from the stub"], frames
    assert frames[-1] == {"type": "stream.end"}, frames


async def test_structured_result_carries_top_level_title(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "structured")
    response_format = {"title": "Ans", "type": "object"}

    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "claude_code", {"user_message": "hi", "response_format": response_format}, retry_on_reloading=True
        )
    # A structured terminal returns its ``data`` object verbatim (never the message text).
    assert result.data == {"title": "stub result", "body": "structured"}, result.data

    frames = await run_sse(stack, {"user_message": "hi", "response_format": response_format})
    structured = frames_of_type(frames, "structured_final")
    assert len(structured) == 1, frames
    assert structured[0]["data"] == {"title": "stub result", "body": "structured"}, structured


async def test_thinking_maps_to_reasoning_step(fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "reasoning")

    frames = await run_sse(stack, {"user_message": "hi"})
    reasoning = frames_of_type(frames, "reasoning_step")
    assert [f["text"] for f in reasoning] == ["considering the request"], frames
    # The reasoning step precedes the final answer in stream order.
    finals = frames_of_type(frames, "message_final")
    assert [f["text"] for f in finals] == ["done"], frames
    assert frames.index(reasoning[0]) < frames.index(finals[0]), frames


@pytest.mark.parametrize("field", ["tools", "presets", "strategy", "llm_provider", "resume_checkpoint_id"])
async def test_unhonored_params_are_refused_loudly(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub, field: str
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "answer")

    # SSE route: an input key that is not a ``ClaudeCodeInput`` field is a loud 400 that names
    # the offending field — never a silent drop that would run with a default.
    url = f"http://{stack.host}:{stack.port_a}{CLAUDE_RUN_PATH}"
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(url, json={"user_message": "hi", field: "x"})
    assert response.status_code == 400, response.text
    assert field in response.text, response.text

    # Tool face: the same unknown key is rejected by the tool-input schema (``extra="forbid"``).
    async with stack.mcp() as mcp:
        with pytest.raises(ToolError) as excinfo:
            await mcp.call_tool("claude_code", {"user_message": "hi", field: "x"}, retry_on_reloading=True)
    assert field in str(excinfo.value), excinfo.value


async def test_model_auth_requires_exactly_one_mode(fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub) -> None:
    # Neither credential set -> loud config error at run start (the stack pins the api-key by
    # default; unset it and set no oauth token).
    neither = claude_stack(fresh_stack, llm_stub, "answer", TAI_AGENTS_CLAUDE_API_KEY="")
    async with neither.mcp() as mcp:
        result = await mcp.call_tool(
            "claude_code", {"user_message": "hi"}, retry_on_reloading=True, raise_on_error=False
        )
    assert result.is_error, result.data
    assert "EXACTLY ONE model credential" in error_text(result), error_text(result)

    # Both credentials set -> equally loud (no silent precedence).
    both = claude_stack(fresh_stack, llm_stub, "answer", TAI_AGENTS_CLAUDE_OAUTH_TOKEN="oauth-tok")
    async with both.mcp() as mcp:
        result = await mcp.call_tool(
            "claude_code", {"user_message": "hi"}, retry_on_reloading=True, raise_on_error=False
        )
    assert result.is_error, result.data
    assert "EXACTLY ONE model credential" in error_text(result), error_text(result)


async def test_each_auth_mode_is_accepted_alone(fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub) -> None:
    # The default stack runs api-key mode; prove it drives a clean turn.
    api_key = claude_stack(fresh_stack, llm_stub, "answer")
    async with api_key.mcp() as mcp:
        result = await mcp.call_tool("claude_code", {"user_message": "hi"}, retry_on_reloading=True)
    assert result.data == "hello from the stub", result.data

    # OAuth mode: unset the api key, set the oauth token — the exactly-one rule holds for it too.
    oauth = claude_stack(
        fresh_stack, llm_stub, "answer", TAI_AGENTS_CLAUDE_API_KEY="", TAI_AGENTS_CLAUDE_OAUTH_TOKEN="oauth-tok"
    )
    async with oauth.mcp() as mcp:
        result = await mcp.call_tool("claude_code", {"user_message": "hi"}, retry_on_reloading=True)
    assert result.data == "hello from the stub", result.data


async def test_bare_tag_session_image_is_refused_loudly(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "answer", TAI_AGENTS_CLAUDE_SESSION_IMAGE="registry.example/img:latest")
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "claude_code", {"user_message": "hi"}, retry_on_reloading=True, raise_on_error=False
        )
    assert result.is_error, result.data
    assert "digest reference" in error_text(result), error_text(result)
