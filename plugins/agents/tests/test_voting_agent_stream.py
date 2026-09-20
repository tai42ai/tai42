"""``voting_agent`` event taxonomy across the ``astream`` face and the ``run``
drain to a voting output.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tai42_contract.agent import Agent
from tai42_contract.agent.events import (
    MessageFinal,
    StructuredFinal,
    ToolCallStep,
)
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._voting_agent_support import (
    AGENT_NAME,
    _collect,
    _fake_full_judge_stream,
    _fake_voter_invoke,
    _make_tool,
)

from tai42_agents.voting_agent import agent as agent_module
from tai42_agents.voting_agent.agent import VotingAgent
from tai42_agents.voting_agent.model import VoteInfo, VoterSpec, VotingOutput


def test_decorator_registers_a_live_voting_agent() -> None:
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    assert isinstance(agent, VotingAgent)
    assert isinstance(agent, Agent)
    assert agent.tool_name == AGENT_NAME
    assert VotingAgent.ToolInput is not None
    assert "voter" in agent.tool_description.lower()


def test_astream_emits_every_kind_then_structured_final(monkeypatch, app_tools, resource_manager) -> None:
    """The judge's per-step events stream in order, then a terminal
    ``StructuredFinal`` carrying the full ``VotingOutput``."""
    app_tools.client_tools["jt"] = _make_tool("jt")
    app_tools.client_tools["vt"] = _make_tool("vt")

    voter_calls: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke(voter_calls))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream(captured))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = asyncio.run(
        _collect(
            agent.astream(
                judge_tools=["jt"],
                voter_tools=["vt"],
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                voters=[VoterSpec(provider="p1", model="m1")],
            )
        )
    )

    assert [e.type for e in events] == [
        "reasoning_step",
        "tool_call_step",
        "tool_result_step",
        "message_delta",
        "message_delta",
        "message_final",
        "run_usage",
        "structured_final",
    ]

    # Judge got the resolved judge tool; the single voter got the resolved voter tool.
    assert [t.name for t in captured["judge_tools"]] == ["jt"]
    assert captured["judge_llm_provider"] == "jp"
    assert len(voter_calls) == 1
    assert [t.name for t in voter_calls[0]["tools"]] == ["vt"]
    assert voter_calls[0]["llm_provider"] == "p1"
    assert voter_calls[0]["llm_kwargs"] == {"model": "m1"}

    final = events[-1]
    assert isinstance(final, StructuredFinal)
    assert isinstance(final.data, VotingOutput)
    # The judge label is the model its run actually reported, not a placeholder.
    assert final.data.judge == VoteInfo(provider="jp", model="judge-model", verdict="VERDICT")
    assert final.data.voters == [VoteInfo(provider="p1", model="m1", verdict="voter-p1")]


def test_astream_event_order_tool_call_final_structured(monkeypatch, app_tools, resource_manager) -> None:
    """The decisive-stage ordering: a judge tool call, the judge's final message,
    then the structured terminal."""

    async def fake_judge(**kwargs: Any):
        yield ToolCallStep(tool="jt", args={}, call_id="c1")
        yield MessageFinal(text="VERDICT")

    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", fake_judge)

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                voters=[VoterSpec(provider="p1", model="m1")],
            )
        )
    )
    assert [e.type for e in events] == ["tool_call_step", "message_final", "structured_final"]
    # No RunUsage in this stream and no judge kwargs, so the judge model is unknown.
    final = events[-1]
    assert isinstance(final, StructuredFinal)
    assert final.data.judge.model is None


def test_astream_threads_user_content_kwargs_to_the_judge_stream(monkeypatch, app_tools, resource_manager) -> None:
    """``user_content_kwargs`` reaches the judge's event stream, marking the judge's
    last user message for caching; the judge system prompt is internal so no
    ``system_content_kwargs`` seat exists."""
    captured: dict[str, Any] = {}

    async def fake_judge(**kwargs: Any):
        captured["user_content_kwargs"] = kwargs.get("user_content_kwargs")
        yield MessageFinal(text="VERDICT")

    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", fake_judge)

    cache = {"cache_control": {"type": "ephemeral"}}
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                user_content_kwargs=cache,
            )
        )
    )
    assert captured["user_content_kwargs"] == cache


def test_run_drains_to_voting_output(monkeypatch, app_tools, resource_manager) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream(captured))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    result = asyncio.run(
        agent.run(
            judge_message=TemplatedText(content="decide"),
            voter_message=TemplatedText(content="answer"),
            judge_llm_provider="jp",
            judge_llm_kwargs={"model": "jm"},
            voters=[VoterSpec(provider="p1", model="m1")],
        )
    )
    assert isinstance(result, VotingOutput)
    # The run reports "judge-model", which is the model that actually ran and so
    # wins over the requested "jm".
    assert result.judge == VoteInfo(provider="jp", model="judge-model", verdict="VERDICT")
    assert [v.verdict for v in result.voters] == ["voter-p1"]
