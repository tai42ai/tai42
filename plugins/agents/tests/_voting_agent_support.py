"""Shared rig for the ``voting_agent`` test modules: the agent name, tool factory,
astream drain, and the scripted voter/judge invoke and stream fakes.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from langchain_core.tools import StructuredTool
from pydantic import BaseModel
from tai42_contract.agent.events import (
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    RunUsage,
    StreamEvent,
    ToolCallStep,
    ToolResultStep,
)

from tai42_agents._internal.usage import AgentInvokeResult, CallUsage
from tai42_agents.voting_agent import agent as agent_module

AGENT_NAME = "voting_agent"


def _make_tool(name: str) -> StructuredTool:
    async def call(**kwargs: Any) -> dict[str, Any]:
        return kwargs

    return StructuredTool.from_function(
        func=None,
        coroutine=call,
        name=name,
        description="t",
        args_schema={"type": "object", "properties": {}, "required": []},
    )


async def _collect(agen: Any) -> list[StreamEvent]:
    return [event async for event in agen]


def _fake_voter_invoke(calls: list[dict[str, Any]], usage_model: str | None = None):
    """A fake ``ainvoke_tools_agent`` that records each voter call and returns an
    output tagged with the voter's provider. ``usage_model`` sets the model the
    run reports back (as a real provider response would); ``None`` mirrors a
    provider that omits the model."""

    async def fake(**kwargs: Any) -> AgentInvokeResult:
        calls.append(kwargs)
        return AgentInvokeResult(
            output=f"voter-{kwargs['llm_provider']}",
            usage=CallUsage(input_tokens=0, output_tokens=0, model=usage_model),
        )

    return fake


def _fake_full_judge_stream(captured: dict[str, Any]):
    """A fake judge stream that records the tools it was handed and yields every
    per-step event kind the projection can produce, ending with the assembled
    ``MessageFinal``."""

    async def fake(**kwargs: Any):
        captured["judge_tools"] = kwargs["tools"]
        captured["judge_llm_provider"] = kwargs["llm_provider"]
        yield ReasoningStep(text="weighing the voters")
        yield ToolCallStep(tool="jt", args={}, call_id="c1")
        yield ToolResultStep(tool="jt", call_id="c1", result="ok")
        yield MessageDelta(text="VER")
        yield MessageDelta(text="DICT")
        yield MessageFinal(text="VERDICT")
        yield RunUsage(input_tokens=1, output_tokens=2, total_tokens=3, model="judge-model")

    return fake


class _NotVotingOutput(BaseModel):
    """A response_format the voting agent does not produce."""

    answer: str


def _fake_checkpoint_judge_stream(captured: dict[str, Any]):
    """A judge stream that records the ``checkpoint_provider`` it was handed."""

    async def fake(**kwargs: Any):
        captured["judge_checkpoint_provider"] = kwargs["checkpoint_provider"]
        yield MessageFinal(text="VERDICT")

    return fake


def _install_reject_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake the voter/judge seams so that if the guard were bypassed the run would
    SUCCEED (returning a VotingOutput) — a dropped reasons key then fails the
    pytest.raises below cleanly rather than erroring on a real LLM call."""
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))


def _fake_overlap_recording_invoke(state: dict[str, Any]):
    """A fake voter that tracks how many voters are concurrently in-flight, so a
    test can assert a bounded semaphore never lets two overlap."""

    async def fake(**kwargs: Any) -> AgentInvokeResult:
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        # Yield to the loop: without the semaphore both gathered voters would be
        # in-flight here at once and ``max_active`` would climb to 2.
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        state["active"] -= 1
        return AgentInvokeResult(
            output=f"voter-{kwargs['llm_provider']}",
            usage=CallUsage(input_tokens=0, output_tokens=0, model=None),
        )

    return fake


def _fake_delayed_invoke(delays: dict[str, float]):
    """A fake voter whose per-provider delay makes voters COMPLETE out of input
    order, so a test can pin that verdict assembly still follows input order."""

    async def fake(**kwargs: Any) -> AgentInvokeResult:
        provider = kwargs["llm_provider"]
        await asyncio.sleep(delays.get(provider, 0.0))
        return AgentInvokeResult(
            output=f"voter-{provider}",
            usage=CallUsage(input_tokens=0, output_tokens=0, model=None),
        )

    return fake
