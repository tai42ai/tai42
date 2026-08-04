"""``refine_agent`` over the stack: the Evaluator<->Critic refinement loop.

The Evaluator drafts, the Critic reviews; the loop runs silently until the Critic
emits the approval token, then the final approved Evaluator pass streams. The
scripted stub plays draft -> approval -> final answer, and the recorded call count
proves all three model turns ran through the real loop."""

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
    # MOCK leg; a real-provider leg is exercised on the e2e creds host (PLAN_2 §F), not in CI,
    # so the stub-bound module steps aside when the 'llm' seam is real. Inert in the default
    # mock run — is_real("llm") is False, so collection is byte-for-byte today's.
    pytest.mark.skipif(
        HarnessSettings().is_real("llm"),
        reason="scripted llm_stub is the 'llm' mock leg; the real leg runs on the e2e creds host (PLAN_2 §F)",
    ),
]

# The Critic approval token the refine loop breaks on
# (``tai42_agents.refine_agent.prompt.CRITIC_APPROVAL_MESSAGE``). It is inlined
# because the SUT agent package cannot be imported into the harness process: its
# module import fires the ``@tai42_app.agents.agent`` decorator, which needs the
# app bound — only ever true inside the served stack, never in this process.
CRITIC_APPROVAL_MESSAGE = "7f9c2d1a"


async def test_refine_agent_loop_to_approval_over_mcp(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    final = f"approved answer {uniq('final')}"
    draft = f"draft {uniq('draft')}"
    llm_stub.reset()
    llm_stub.script(
        [
            # Evaluator iteration 0: a draft.
            {"content": draft},
            # Critic iteration 0: emit the exact approval token -> loop breaks.
            {"content": f"looks good {CRITIC_APPROVAL_MESSAGE}"},
            # Final approved Evaluator pass (the streamed stage).
            {"content": final},
        ]
    )

    async with agents_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "refine_agent",
            {"evaluator_message": "write a haiku", "critic_message": "review the haiku"},
        )

    assert final in json.dumps(result.data), f"refine_agent did not return the final approved answer: {result.data}"
    # Draft + critic approval + final approved pass = three model round-trips.
    assert len(llm_stub.requests) == 3, f"expected 3 LLM round-trips, saw {len(llm_stub.requests)}"
    # The Evaluator→Critic hand-off is the agent's distinctive wiring: the critic is
    # asked to review the DRAFT the evaluator just produced, not the original prompt.
    assert draft in json.dumps(llm_stub.requests[1]), "the evaluator's draft never reached the critic"
