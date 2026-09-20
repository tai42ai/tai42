"""``voting_agent`` voter orchestration: provider selection, delivery scoping,
model resolution, over-limit guard, concurrency, and verdict ordering.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.agent.events import (
    StructuredFinal,
)
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._delivery_scope import assert_delivery_scoped, probe_tool
from tests._voting_agent_support import (
    AGENT_NAME,
    _collect,
    _fake_delayed_invoke,
    _fake_full_judge_stream,
    _fake_overlap_recording_invoke,
    _fake_voter_invoke,
    _make_tool,
)

from tai42_agents.voting_agent import agent as agent_module
from tai42_agents.voting_agent.model import VoteInfo, VoterSpec


def test_default_voter_uses_judge_provider(monkeypatch, app_tools, resource_manager) -> None:
    """With no ``voters``, a single voter runs on the judge's provider and
    kwargs, and its model label is ``None`` when nothing names a model."""
    voter_calls: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke(voter_calls))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream(captured))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
            )
        )
    )
    assert len(voter_calls) == 1
    assert voter_calls[0]["llm_provider"] == "jp"
    final = events[-1]
    assert isinstance(final, StructuredFinal)
    assert final.data.voters == [VoteInfo(provider="jp", model=None, verdict="voter-jp")]


def test_judge_and_voter_tools_are_delivery_scoped(monkeypatch, app_tools, resource_manager) -> None:
    """A tool a judge or a voter dispatches is a STEP of the voting turn, never a second
    answerer of it: it must not read the completion binding addressing the agent's own
    deferred answer off the contextvar."""
    judge_probe, judge_seen = probe_tool("jt")
    voter_probe, voter_seen = probe_tool("vt")
    app_tools.client_tools["jt"] = judge_probe
    app_tools.client_tools["vt"] = voter_probe

    voter_calls: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke(voter_calls))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream(captured))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    asyncio.run(
        _collect(
            agent.astream(
                judge_tools=["jt"],
                voter_tools=["vt"],
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                voters=[VoterSpec(provider="p1", model="m1")],
            )
        )
    )

    assert_delivery_scoped(captured["judge_tools"][0], judge_seen)
    assert_delivery_scoped(voter_calls[0]["tools"][0], voter_seen)


def test_multiple_judge_tools_flow_through_as_a_list(monkeypatch, app_tools, resource_manager) -> None:
    """A list of judge tools resolves and reaches the judge stream as a
    ``list[StructuredTool] | None`` — carrying every tool, not just one."""
    app_tools.client_tools["jt1"] = _make_tool("jt1")
    app_tools.client_tools["jt2"] = _make_tool("jt2")

    captured: dict[str, Any] = {}
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream(captured))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    asyncio.run(
        _collect(
            agent.astream(
                judge_tools=["jt1", "jt2"],
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                voters=[VoterSpec(provider="p1", model="m1")],
            )
        )
    )
    assert isinstance(captured["judge_tools"], list)
    assert [t.name for t in captured["judge_tools"]] == ["jt1", "jt2"]


def test_duplicate_provider_voters_each_run(monkeypatch, app_tools, resource_manager) -> None:
    """Two voter specs naming the SAME provider each spawn their own voter — the
    list shape expresses duplicates a provider-keyed dict would have collapsed —
    and each records its own pinned model in order."""
    voter_calls: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke(voter_calls))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream(captured))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                voters=[
                    VoterSpec(provider="p1", model="a"),
                    VoterSpec(provider="p1", model="b"),
                ],
            )
        )
    )
    assert len(voter_calls) == 2
    assert [c["llm_provider"] for c in voter_calls] == ["p1", "p1"]
    assert [c["llm_kwargs"] for c in voter_calls] == [{"model": "a"}, {"model": "b"}]
    final = events[-1]
    assert isinstance(final, StructuredFinal)
    voters = final.data.voters
    assert voters == [
        VoteInfo(provider="p1", model="a", verdict="voter-p1"),
        VoteInfo(provider="p1", model="b", verdict="voter-p1"),
    ]


def test_voter_model_prefers_run_reported_model(monkeypatch, app_tools, resource_manager) -> None:
    """When the voter run reports the model it actually ran, that real model is
    the label — winning over the model the spec pinned."""
    voter_calls: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        agent_module, "ainvoke_tools_agent", _fake_voter_invoke(voter_calls, usage_model="resolved-model")
    )
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream(captured))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                voters=[VoterSpec(provider="p1", model="requested")],
            )
        )
    )
    final = events[-1]
    assert isinstance(final, StructuredFinal)
    assert final.data.voters == [VoteInfo(provider="p1", model="resolved-model", verdict="voter-p1")]


def test_voter_spec_model_kwarg_folds_into_llm_kwargs(monkeypatch, app_tools, resource_manager) -> None:
    """A spec's ``llm_kwargs`` reach the voter run, and the explicit ``model``
    field is folded into them and takes precedence over any ``model`` inside
    ``llm_kwargs``."""
    voter_calls: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke(voter_calls))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream(captured))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                voters=[VoterSpec(provider="p1", model="explicit", llm_kwargs={"model": "shadowed", "temperature": 0})],
            )
        )
    )
    assert voter_calls[0]["llm_kwargs"] == {"model": "explicit", "temperature": 0}
    final = events[-1]
    assert isinstance(final, StructuredFinal)
    assert final.data.voters == [VoteInfo(provider="p1", model="explicit", verdict="voter-p1")]


def test_over_limit_voters_raise_before_any_llm_call(monkeypatch, app_tools, resource_manager) -> None:
    """A voters list longer than ``max_voters`` raises a ``ValueError`` naming the
    env var BEFORE any voter LLM runs — the mocked ``ainvoke_tools_agent`` is never
    invoked."""
    monkeypatch.setattr(
        agent_module, "agents_limits_settings", lambda: SimpleNamespace(max_voters=2, voter_concurrency=8)
    )
    voter_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke(voter_calls))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="TAI_AGENTS_MAX_VOTERS"):
        asyncio.run(
            _collect(
                agent.astream(
                    judge_message=TemplatedText(content="decide"),
                    voter_message=TemplatedText(content="answer"),
                    judge_llm_provider="jp",
                    voters=[VoterSpec(provider="p1"), VoterSpec(provider="p2"), VoterSpec(provider="p3")],
                )
            )
        )
    assert voter_calls == []


def test_voter_concurrency_one_never_overlaps(monkeypatch, app_tools, resource_manager) -> None:
    """With ``voter_concurrency`` forced to 1, two voters run strictly one at a
    time — the recorded peak in-flight count never exceeds 1."""
    monkeypatch.setattr(
        agent_module, "agents_limits_settings", lambda: SimpleNamespace(max_voters=16, voter_concurrency=1)
    )
    state: dict[str, Any] = {"active": 0, "max_active": 0}
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_overlap_recording_invoke(state))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                voters=[VoterSpec(provider="p1"), VoterSpec(provider="p2")],
            )
        )
    )
    assert state["max_active"] == 1


def test_verdict_order_matches_input_order_despite_completion_order(monkeypatch, app_tools, resource_manager) -> None:
    """Voters finish in reverse of input order, yet the assembled verdicts stay in
    input order — the bounded fan-out preserves index alignment."""
    monkeypatch.setattr(
        agent_module, "agents_limits_settings", lambda: SimpleNamespace(max_voters=16, voter_concurrency=8)
    )
    # p1 finishes last, p3 first — completion order is the reverse of input order.
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_delayed_invoke({"p1": 0.03, "p2": 0.015, "p3": 0.0}))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                voters=[VoterSpec(provider="p1"), VoterSpec(provider="p2"), VoterSpec(provider="p3")],
            )
        )
    )
    final = events[-1]
    assert isinstance(final, StructuredFinal)
    assert [v.provider for v in final.data.voters] == ["p1", "p2", "p3"]
    assert [v.verdict for v in final.data.voters] == ["voter-p1", "voter-p2", "voter-p3"]
