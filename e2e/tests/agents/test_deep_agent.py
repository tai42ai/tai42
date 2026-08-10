"""``deep_agent`` over the stack: the plan -> subagent -> synthesis loop.

The main agent delegates to a declared subagent through its built-in ``task``
tool (deepagents' ``SubAgentMiddleware``); the subagent runs its own graph and
returns a result, which the main agent then synthesizes into a final answer. The
scripted stub plays all three model turns in order (main delegates, subagent
answers, main synthesizes), and the recorded turns prove the subagent's output
flowed back into the main agent's synthesis."""

from __future__ import annotations

import json
from collections.abc import Callable

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


async def test_deep_agent_plan_subagent_synthesis_over_mcp(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    finding = f"finding {uniq('sub')}"
    synthesis = f"synthesis {uniq('final')}"
    llm_stub.reset()
    llm_stub.script(
        [
            # Main agent: delegate to the researcher subagent via the task tool.
            {
                "tool_call": {
                    "name": "task",
                    "arguments": {"description": "research the topic", "subagent_type": "researcher"},
                }
            },
            # Researcher subagent: return its finding (no tool calls -> its graph ends).
            {"content": finding},
            # Main agent: synthesize the subagent's finding into the final answer.
            {"content": synthesis},
        ]
    )

    subagents = [
        {
            "name": "researcher",
            "description": "Researches a topic and reports findings.",
            "system_prompt": "You research topics and report concise findings.",
        }
    ]

    async with agents_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "deep_agent",
            {"user_message": "research and summarize the topic", "subagents": subagents},
        )

    assert synthesis in json.dumps(result.data), f"deep_agent did not return the synthesis: {result.data}"
    requests = llm_stub.requests
    # Plan (delegate) + subagent + synthesis = three model round-trips.
    assert len(requests) == 3, f"expected 3 LLM round-trips, saw {len(requests)}"
    # The subagent's finding flowed back into the main agent's synthesis turn.
    assert finding in json.dumps(requests[2]), "the subagent finding never reached the main agent's synthesis"
