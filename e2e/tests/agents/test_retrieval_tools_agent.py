"""``retrieval_tools_agent`` over the stack: on-demand vector-store retrieval.

The agent embeds each resolved tool's description into a langgraph store (the
in-process ``memory`` store here) and exposes a single ``retrieve_tools`` semantic
search. The model asks for a tool by describing it; the store returns the match,
which is surfaced back to the model as an ``Available tools`` message. Embedding
runs entirely offline against the stub's deterministic ``/v1/embeddings``; the
scripted turns drive retrieve -> terminal, and the recorded second request proves
the retrieved tool reached the model."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.stack import TaiStack

# The agents stack runs no backend worker; skip this module on non-default
# backend legs (they exercise no backend seam).
pytestmark = pytest.mark.backendless


async def test_retrieval_agent_embeds_and_retrieves_over_mcp(
    agents_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    result_text = f"the answer is {uniq('res')}"
    llm_stub.reset()
    llm_stub.script(
        [
            # Step 1: ask the semantic search for a tool by describing it.
            {"tool_call": {"name": "retrieve_tools", "arguments": {"query": "echo the payload back"}}},
            # Step 2: the terminal status envelope the routing node requires.
            {"content": json.dumps({"status": "success", "message": "done", "result": result_text})},
        ]
    )

    async with agents_stack.mcp() as mcp:
        result = await mcp.call_tool(
            "retrieval_tools_agent",
            {"user_message": "echo something for me", "tool_names": ["e2e_echo"]},
        )

    assert result_text in json.dumps(result.data), f"retrieval agent did not return the terminal result: {result.data}"
    requests = llm_stub.requests
    assert len(requests) == 2, f"expected 2 LLM round-trips, saw {len(requests)}"
    # The retrieved tool was surfaced back to the model as an "Available tools"
    # message on the second turn — proof the store round-trip reached the LLM.
    assert "e2e_echo" in json.dumps(requests[1]), "the retrieved tool never reached the model"
