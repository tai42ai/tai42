"""§3b — the ``claude_code`` ask/park boundary and the turn-budget door.

The reachable, engine-agnostic legs over the identity-less ephemeral doors:

* THE TURN-BUDGET DOOR — a live-caller ``claude_code`` turn on the SSE run route (a held
  client connection, so NOT detached-exempt) with ``TAI_TURN_TIMEOUT_SECONDS`` set and the
  STALLING runner errs LOUDLY at the budget: the shared live-caller drive seam raises on
  expiry and the run surfaces a ``stream.error`` naming the timeout. The stalling runner never
  emits a terminal, so the budget — not the runner — ends the turn.
* NO ASYNC PARK ON AN EPHEMERAL RUN — the async ask requires a durable (threaded) run to rebind
  the continuation onto; a thread-less tool-face/SSE run cannot park, so the run terminates
  WITHOUT a ``suspended_final`` frame (never a stranded question).
* THE ASK-DESIGN PREMISE — a platform ``ask_user(mode="async")`` invoked over the REAL MCP edge
  (no resuming driver) raises "async ask requires a resuming driver", which is exactly why the
  adapter answers a SYNC ask itself and parks an ASYNC ask through the resume continuation
  rather than leaning on the MCP edge. This keeps the ask design's justification ENFORCED.

The conversation-bridge ask legs the plan also names — an adapter-answered SYNC ask, an ASYNC
PARK + cross-door resume, a FIRST-TURN park — require the identity-bound conversation-bridge
door (a trusted in-process ``thread_id`` + a bound execution identity + the resume store). That
door is not wired on ``build_claude_agent_stack``; those legs are covered in the plugin's own
drive tests, and are skipped here with that reason so the gap is explicit, not silent.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

from ._claude_support import claude_stack, frames_of_type, run_sse

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        HarnessSettings().is_real("claude_agent"),
        reason="scripted runner stub is the 'claude_agent' mock leg; the real turn is the §5 smoke",
    ),
]


async def test_stalling_turn_errs_at_the_turn_budget_over_sse(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    # A short turn budget plus the stalling runner: the live-caller SSE drive is budgeted, so
    # the turn ends LOUDLY at the timeout rather than running to the (much larger) exec ceiling.
    stack = claude_stack(fresh_stack, llm_stub, "stall", TAI_TURN_TIMEOUT_SECONDS="3")
    frames = await run_sse(stack, {"user_message": "hi"})
    errors = frames_of_type(frames, "stream.error")
    assert errors, frames
    assert "turn timeout" in errors[0]["message"], errors


async def test_async_ask_on_an_ephemeral_run_does_not_park(
    fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub
) -> None:
    stack = claude_stack(fresh_stack, llm_stub, "ask_async")
    frames = await run_sse(stack, {"user_message": "hi"})
    # A thread-less run cannot rebind an async continuation, so it never parks.
    assert not frames_of_type(frames, "suspended_final"), frames
    assert frames[-1] == {"type": "stream.end"}, frames


async def test_async_ask_over_the_mcp_edge_is_refused(fresh_stack: Callable[..., TaiStack], llm_stub: LlmStub) -> None:
    # The premise behind the adapter-side ask design: a platform async ask on the MCP edge has
    # no resuming driver and raises loudly, so an agent must not lean on the edge to park.
    stack = claude_stack(fresh_stack, llm_stub, "answer")
    async with stack.mcp() as mcp:
        result = await mcp.call_tool(
            "e2e_agent_async_ask",
            {"question": "proceed?", "expiry_seconds": 60},
            retry_on_reloading=True,
            raise_on_error=False,
        )
    assert result.is_error, result.data
    text = next((getattr(part, "text", "") for part in (result.content or [])), "")
    assert "async ask requires a resuming driver" in text, text


@pytest.mark.skip(
    reason="the adapter-answered sync ask, the async park + cross-door resume, and the first-turn "
    "park require the identity-bound conversation-bridge door (a trusted in-process thread_id + a "
    "bound execution identity + the resume store), which build_claude_agent_stack does not wire; "
    "these are covered in the plugin's own drive tests"
)
async def test_conversation_bridge_ask_and_park_legs() -> None:  # pragma: no cover - documented gap
    ...
