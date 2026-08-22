"""§5 real-vendor smoke — ONE real ``langchain_deep_agent`` model turn over the fake persistent
sandbox.

Gated on the single ``.env`` ``ANTHROPIC_API_KEY`` (the ``claude_agent`` real seam): when
selected, ``build_deep_agent_durable_stack`` repoints its LLM group at Anthropic keyed off that
key, so the deep agent's ONE model turn runs SERVER-side through ``get_llm_async`` to real
Anthropic. The MODEL credential NEVER enters the session (only service creds do, §B4) — no
``claude-agent-sdk``, no in-session model key. Asserts a terminal answer lands.

SKIPS without the key — expected off the creds host. The loud-fail selection wiring names a
missing ``ANTHROPIC_API_KEY`` if the seam is selected without it (``assert_real_selection_ready``).
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

from ._support import AGENT

pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        not HarnessSettings().is_real("claude_agent"),
        reason="the real langchain_deep_agent smoke needs the .env ANTHROPIC_API_KEY (claude_agent real seam)",
    ),
]


async def test_deep_agent_real_model_turn_lands_a_terminal_answer(
    deep_agent_durable_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    """One real model turn: the deep agent answers a trivial prompt through the server-side
    Anthropic LLM seam and a terminal answer comes back over the durable stack."""
    async with deep_agent_durable_stack.mcp(port=deep_agent_durable_stack.port_a) as mcp:
        result = await mcp.call_tool(
            AGENT,
            {"user_message": "Reply with exactly the single word: pong"},
            retry_on_reloading=True,
        )
    # A non-empty terminal answer came back (the exact text is the model's; assert it is a
    # real, non-empty terminal rather than a park/empty run).
    answer = json.dumps(result.data)
    assert result.data, f"the real deep-agent turn produced no terminal answer: {result!r}"
    assert "suspended" not in answer, f"the trivial real turn parked instead of answering: {answer}"
