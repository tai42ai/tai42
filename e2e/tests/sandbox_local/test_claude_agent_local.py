"""``claude_code`` over the REAL direct/host ``sandbox-local`` provider (GATED).

Direct-mode ``claude_code``: the adapter materializes ``tai_runner`` into the host-dir
workspace and drives the operator-installed ``claude-agent-sdk`` wheel (which bundles the
Claude Code CLI — no separate Node.js) against the model credential directly on the host.
The materialize + ``exec_start`` env/cwd paths are all built from
``session.workspace_path`` (here ``<SANDBOX_LOCAL_ROOT>/<workspace_key>``, NO ``/workspace``),
so this leg exercises the workspace-relative resolution and would FAIL under the old
hardcoded ``/workspace`` env — proving the adapter is genuinely provider-agnostic.

GATED on the creds host: it needs the operator-installed SDK wheel + a live model
credential (the single ``.env`` ``ANTHROPIC_API_KEY`` mapped onto ``TAI_AGENTS_CLAUDE_API_KEY``
by the stack builder). It SKIPS by default and runs only under ``TAI_E2E_REAL=claude_agent``;
an absent runtime on the gated host surfaces LOUDLY, never a silent skip past the gate. This
is the direct-mode counterpart to the fake-sandbox real-SDK smoke (§5) — with
``test_deep_agent_local.py``, BOTH agents are proven runnable under ``sandbox-local``."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

# Direct-mode claude_code needs the operator-installed claude-agent-sdk wheel + a live model
# credential on the host, so this leg runs ONLY on the creds host (metered API-key mode).
pytestmark = [
    pytest.mark.backendless,
    pytest.mark.skipif(
        not HarnessSettings().is_real("claude_agent"),
        reason="direct-mode claude_code needs a host claude-agent-sdk runtime + ANTHROPIC_API_KEY",
    ),
]


async def test_claude_agent_smoke_over_the_local_provider(
    sandbox_local_claude_stack: TaiStack, uniq: Callable[[str], str]
) -> None:
    token = uniq("ack")

    async with sandbox_local_claude_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "claude_code",
            {"user_message": f"Reply with exactly this word and nothing else: {token}"},
        )

    # One trivial-prompt smoke: a MessageFinal lands — the drained final answer is a non-empty
    # string carrying the requested token, proving the real SDK ran to completion in the
    # host-dir workspace under the direct provider.
    answer = json.dumps(result.data)
    assert token in answer, f"the claude_code run over sandbox-local did not deliver a MessageFinal: {result.data}"
