"""The production-default langgraph ``redis`` checkpoint/store provider.

The agents stacks elsewhere pin the in-process ``memory`` provider; this module
covers the production default (``LLM_PROVIDER_CHECKPOINT``/``STORE=redis``) against
a module-capable Redis (RediSearch + RedisJSON). The stack is REPLICAS — two
one-worker masters on two ports — so a conversation checkpointed by replica A can
be resumed by replica B, proving the checkpoint state crosses the process
boundary through Redis rather than living in one worker's memory.

The public resume door is the tools_agent JSON tool-face: its ``langgraph_config``
input carries ``configurable.thread_id``, which ``ToolsAgent.run`` forwards
unmodified into the run config, and the compiled agent's redis checkpointer keys
its saved state on that thread id. A second run pinning the same thread id — on a
different replica — resumes the first run's history.

Gated on ``TAI_E2E_CHECKPOINT_REDIS_URL`` via the ``agents_redis_stack`` fixture,
which skips with the ``docker compose --profile agents-redis up -d`` hint when the
module-capable Redis is absent.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest
import redis

from tai42_e2e.llmstub import LlmStub
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack


async def _run_sse(stack: TaiStack, port: int, path: str, body: dict) -> list[str]:
    """POST an agent run over the SSE run door on ``port`` and return the ``data:``
    frames, draining the stream so the run completes (and its checkpoint lands)
    before returning."""
    url = f"http://{stack.host}:{port}{path}"
    frames: list[str] = []
    async with httpx.AsyncClient(timeout=15.0) as client, client.stream("POST", url, json=body) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.startswith("data:"):
                frames.append(line[len("data:") :].strip())
    return frames


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


async def test_redis_checkpoint_resumes_across_replicas(
    agents_redis_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """A thread checkpointed via replica A resumes with full history via replica
    B — the cross-worker redis checkpoint seam over the public run door."""
    thread_id = uniq("thread")
    name_token = uniq("name")
    llm_stub.reset()
    llm_stub.script(
        [
            {"content": f"Nice to meet you, {name_token}."},
            {"content": f"Your name is {name_token}."},
        ]
    )
    langgraph_config = {"configurable": {"thread_id": thread_id}}

    # Turn 1 on replica A: state the name under a pinned thread id.
    async with agents_redis_stack.mcp(port=agents_redis_stack.port_a) as mcp_a:
        await mcp_a.call_tool(
            "tools_agent",
            {"user_message": f"my name is {name_token}", "langgraph_config": langgraph_config},
        )

    # Turn 2 on replica B: same thread id, a question that only the restored
    # history can answer.
    async with agents_redis_stack.mcp(port=agents_redis_stack.port_b) as mcp_b:
        await mcp_b.call_tool(
            "tools_agent",
            {"user_message": "what is my name?", "langgraph_config": langgraph_config},
        )

    requests = llm_stub.requests
    assert len(requests) == 2, f"expected 2 LLM round-trips, saw {len(requests)}"
    # Replica B's model call carried replica A's turn — the checkpoint restored the
    # conversation across the process boundary. Without cross-worker persistence
    # the second call would carry only "what is my name?".
    assert name_token in json.dumps(requests[1]), (
        "replica B did not see replica A's checkpointed history; the redis checkpoint did not cross workers"
    )
    # The state genuinely lives in the checkpoint Redis (not silently in memory):
    # its logical DB holds checkpoint keys after the run.
    checkpoint_url = agents_redis_stack.resources.checkpoint_redis_url
    assert checkpoint_url is not None  # the module skips unless the checkpoint Redis is configured
    client = redis.Redis.from_url(checkpoint_url, decode_responses=True)
    try:
        keys = client.keys("checkpoint*")
    finally:
        client.close()
    assert keys, "no checkpoint keys landed in the checkpoint Redis; the redis provider was not exercised"


async def test_redis_checkpoint_resumes_across_replicas_over_sse_run_door(
    agents_redis_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """The SSE run door (``POST /api/agents/{name}/runs``, which drives
    ``astream``) honors ``langgraph_config``'s ``configurable.thread_id`` — a
    thread pinned on replica A resumes with full history on replica B, in parity
    with the MCP tool-face. Guards against the streaming face silently dropping
    the caller's thread pin."""
    thread_id = uniq("thread")
    name_token = uniq("name")
    llm_stub.reset()
    llm_stub.script(
        [
            {"content": f"Nice to meet you, {name_token}."},
            {"content": f"Your name is {name_token}."},
        ]
    )
    langgraph_config = {"configurable": {"thread_id": thread_id}}
    path = "/api/agents/tools_agent/runs"

    frames_a = await _run_sse(
        agents_redis_stack,
        agents_redis_stack.port_a,
        path,
        {"user_message": f"my name is {name_token}", "langgraph_config": langgraph_config},
    )
    assert any(name_token in frame for frame in frames_a), f"replica A run did not complete: {frames_a}"

    await _run_sse(
        agents_redis_stack,
        agents_redis_stack.port_b,
        path,
        {"user_message": "what is my name?", "langgraph_config": langgraph_config},
    )

    requests = llm_stub.requests
    assert len(requests) == 2, f"expected 2 LLM round-trips, saw {len(requests)}"
    # Replica B's model call carried replica A's turn — the SSE run door pinned the
    # thread and the redis checkpoint restored the conversation across workers.
    assert name_token in json.dumps(requests[1]), (
        "the SSE run door dropped the langgraph_config thread pin; replica B did not resume replica A's history"
    )


async def test_redis_store_round_trip_through_stack(
    agents_redis_stack: TaiStack, llm_stub: LlmStub, uniq: Callable[[str], str]
) -> None:
    """The retrieval agent embeds a tool description into the langgraph redis
    STORE and searches it back — the redis store provider round-trips end to end
    through the stack (RediSearch vector index on the module-capable Redis)."""
    result_text = f"stored answer {uniq('res')}"
    llm_stub.reset()
    llm_stub.script(
        [
            {"tool_call": {"name": "retrieve_tools", "arguments": {"query": "echo the payload back"}}},
            {"content": json.dumps({"status": "success", "message": "done", "result": result_text})},
        ]
    )

    async with agents_redis_stack.mcp(port=agents_redis_stack.port_a) as mcp:
        result = await mcp.call_tool(
            "retrieval_tools_agent",
            {"user_message": "echo something for me", "tool_names": ["e2e_echo"]},
        )

    assert result_text in json.dumps(result.data), f"retrieval agent did not return the terminal result: {result.data}"
    requests = llm_stub.requests
    assert len(requests) == 2, f"expected 2 LLM round-trips, saw {len(requests)}"
    assert "e2e_echo" in json.dumps(requests[1]), "the tool retrieved from the redis store never reached the model"
    # The round-trip genuinely went through the langgraph REDIS store, not silently
    # through the memory provider: the store's keys landed in this stack's checkpoint
    # Redis DB. Without this the assertion above passes under LLM_PROVIDER_STORE=memory.
    checkpoint_url = agents_redis_stack.resources.checkpoint_redis_url
    assert checkpoint_url is not None  # the module skips unless the checkpoint Redis is configured
    client = redis.Redis.from_url(checkpoint_url, decode_responses=True)
    try:
        store_keys = client.keys("store*")
    finally:
        client.close()
    assert store_keys, "no store keys landed in the checkpoint Redis; the redis store provider was not exercised"
