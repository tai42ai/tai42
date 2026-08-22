"""§5 — the ONE real ``claude-agent-sdk`` smoke turn inside the process-based fake sandbox.

Gated on ``is_real("claude_agent")`` (a single ``.env`` ``ANTHROPIC_API_KEY``): the
``claude_agent_stack`` builder maps that key onto the plugin's operator auth env
(``TAI_AGENTS_CLAUDE_API_KEY``, the metered api-key mode) and sets ``SANDBOX_FAKE_RUNNER=real``
so the fake sandbox runs the ACTUAL materialized ``tai_runner`` payload (the real Claude Agent
SDK) instead of the scripted stub — a real engine, NO docker. One trivial prompt, asserting a
real ``MessageFinal`` lands.

Without the key this SKIPS (the inverse gate), which is the expected local/CI outcome; it runs
only on the creds host. API-KEY MODE ONLY for the smoke — the OAuth mode is deterministic-suite
only (no live-subscription smoke).
"""

from __future__ import annotations

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

pytestmark = pytest.mark.skipif(
    not HarnessSettings().is_real("claude_agent"),
    reason="the real claude-agent-sdk smoke needs ANTHROPIC_API_KEY (TAI_E2E_REAL=claude_agent); creds host only",
)


async def test_real_claude_turn_lands_a_message_final(claude_agent_stack: TaiStack) -> None:
    async with claude_agent_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "claude_code",
            {"user_message": "Reply with exactly the single word: pong"},
            retry_on_reloading=True,
        )
    # A real terminal answer drains to a non-empty message string (the contract terminal rule);
    # the exact wording is the model's, so the smoke asserts a MessageFinal landed, not its text.
    assert isinstance(result.data, str), result.data
    assert result.data.strip(), result.data
