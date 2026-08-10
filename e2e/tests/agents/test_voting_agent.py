"""``voting_agent`` over the stack: parallel voters + a deciding judge.

With no explicit voter list a single voter runs on the judge's provider; it
answers the voter prompt, then the judge weighs the verdicts and decides. The
scripted stub plays one voter verdict then the judge decision, and the returned
``VotingOutput`` carries both — proving the voter->judge assembly ran through the
real agent."""

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


async def test_voting_agent_voter_then_judge_over_mcp(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    voter_verdict = f"vote {uniq('voter')}"
    judge_decision = f"decision {uniq('judge')}"
    llm_stub.reset()
    llm_stub.script(
        [
            # The single default voter answers the voter prompt.
            {"content": voter_verdict},
            # The judge weighs the verdicts and decides.
            {"content": judge_decision},
        ]
    )

    async with agents_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "voting_agent",
            {"judge_message": "pick the best answer", "voter_message": "answer the question"},
        )

    payload = json.dumps(result.data)
    assert judge_decision in payload, f"judge verdict missing from VotingOutput: {result.data}"
    assert voter_verdict in payload, f"voter verdict missing from VotingOutput: {result.data}"
    # One voter + one judge = two model round-trips.
    assert len(llm_stub.requests) == 2, f"expected 2 LLM round-trips, saw {len(llm_stub.requests)}"
