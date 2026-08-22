"""§3b/§3f — ``claude_code`` fails CLOSED and raises LOUDLY.

Three loud-failure invariants, all engine-agnostic and driven deterministically over the fake
sandbox:

* FAIL-CLOSED TOOLS ON AN IDENTITY-LESS DOOR — a run with a non-empty ``tool_names`` on a door
  with NO bound execution identity (the tool face and the SSE run route are both anonymous on
  this stack) is refused at the door. A code-execution agent must never proxy a tool the
  platform cannot entitlement-check; the plugin's default is stricter than the platform norm.
* A MALFORMED FRAME IS A LOUD PROTOCOL ERROR — the adapter is the only parser, so a frame
  carrying an unknown protocol version surfaces as a ``stream.error`` (never a silent skip).
* A RUNNER ``fatal`` FRAME RAISES — a runner-side fatal is re-raised to the caller, never
  degraded to a partial answer.

The positive of the tools fence — bridging DOES work on an identity-bound door — needs an
execution identity, which ``build_claude_agent_stack`` does not bind (auth off, no identity
provider); it is covered in the plugin's own drive tests. The SDK-version mismatch gate (the
``hello`` version guard) is exercised here by the malformed frame's loud protocol error; a
distinct ``9.9.9`` hello cannot be produced by the stub because the fake provider injects the
adapter's own pinned version into the runner env, so stub and adapter always agree on it.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

from ._claude_support import claude_stack, error_text, frames_of_type, run_sse

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("claude_agent"),
        reason="scripted runner stub is the 'claude_agent' mock leg; the real turn is the §5 smoke",
    ),
]


async def test_tool_names_on_identity_less_tool_face_is_refused(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "tool_call")
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "claude_code",
            {"user_message": "hi", "tool_names": ["e2e_echo"]},
            retry_on_reloading=True,
            raise_on_error=False,
        )
    assert result.is_error, result.data
    assert "no bound execution identity" in error_text(result), error_text(result)


async def test_tool_names_on_identity_less_sse_route_is_refused(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "tool_call")
    frames = await run_sse(stack, {"user_message": "hi", "tool_names": ["e2e_echo"]})
    errors = frames_of_type(frames, "stream.error")
    assert errors, frames
    assert "no bound execution identity" in errors[0]["message"], errors


async def test_malformed_frame_is_a_loud_protocol_error(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "malformed")
    frames = await run_sse(stack, {"user_message": "hi"})
    errors = frames_of_type(frames, "stream.error")
    assert errors, frames
    assert "protocol version" in errors[0]["message"], errors


async def test_runner_fatal_frame_raises_loudly(fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "fatal")
    frames = await run_sse(stack, {"user_message": "hi"})
    errors = frames_of_type(frames, "stream.error")
    assert errors, frames
    assert "fatal" in errors[0]["message"].lower(), errors
