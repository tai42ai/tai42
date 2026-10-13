"""``voting_agent`` contract surface: response-format acceptance/rejection,
reject-unhonored guards, and the input/model validation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.agent.events import (
    StructuredFinal,
)
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._voting_agent_support import (
    AGENT_NAME,
    _collect,
    _fake_checkpoint_judge_stream,
    _fake_full_judge_stream,
    _fake_voter_invoke,
    _install_reject_fakes,
    _NotVotingOutput,
    script_voting_graphs,
)

from tai42_agents.voting_agent import agent as agent_module
from tai42_agents.voting_agent.agent import VotingAgentInput
from tai42_agents.voting_agent.model import VoterSpec, VotingOutput


@pytest.fixture(autouse=True)
def _scripted_graphs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The voting agent builds its graphs through a double that compiles nothing."""
    script_voting_graphs(monkeypatch)


def test_checkpoint_provider_reaches_the_seam_on_both_faces(monkeypatch, app_tools, resource_manager) -> None:
    """``checkpoint_provider`` is the one ABC param the voting runtime honors: it
    reaches both the voter invocations and the judge stream on ``astream`` and,
    identically, when ``run`` drains that same stream."""
    voter_calls: list[dict[str, Any]] = []
    captured: dict[str, Any] = {}
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke(voter_calls))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_checkpoint_judge_stream(captured))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                checkpoint_provider="cp",
            )
        )
    )
    assert captured["judge_checkpoint_provider"] == "cp"
    assert voter_calls[0]["checkpoint_provider"] == "cp"

    voter_calls.clear()
    captured.clear()
    asyncio.run(
        agent.run(
            judge_message=TemplatedText(content="decide"),
            voter_message=TemplatedText(content="answer"),
            judge_llm_provider="jp",
            checkpoint_provider="cp",
        )
    )
    assert captured["judge_checkpoint_provider"] == "cp"
    assert voter_calls[0]["checkpoint_provider"] == "cp"


def test_voting_output_response_format_is_accepted_on_both_faces(monkeypatch, app_tools, resource_manager) -> None:
    """Passing the ``VotingOutput`` the agent already produces is redundant but
    accepted on both faces — it is not a foreign structured type."""
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = asyncio.run(
        _collect(
            agent.astream(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                judge_llm_provider="jp",
                response_format=VotingOutput,
            )
        )
    )
    assert isinstance(events[-1], StructuredFinal)

    result = asyncio.run(
        agent.run(
            judge_message=TemplatedText(content="decide"),
            voter_message=TemplatedText(content="answer"),
            judge_llm_provider="jp",
            response_format=VotingOutput,  # type: ignore[arg-type]
        )
    )
    assert isinstance(result, VotingOutput)


# The contract keys the voting runtime cannot honor, each with a set value; a foreign
# ``response_format`` is one of them (``VotingOutput`` is accepted).
_UNHONORED_CASES = [
    ("tool_names", ["x"]),
    ("user_message", "usr"),
    ("thread_id", "t1"),
    ("response_format", _NotVotingOutput),
]

# Keywords the voting runtime does not declare, refused by presence.
_UNDECLARED_CASES = [
    ("tools", [object()]),
    ("system_message", "sys"),
    ("strategy", ""),
    ("resume", False),
    ("recursion_limit", 0),
    ("llm_provider", "openai"),
    ("system_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
]


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_both_faces_refuse_a_contract_key_they_cannot_honor(
    monkeypatch, app_tools, resource_manager, param, value
) -> None:
    _install_reject_fakes(monkeypatch)
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    match = rf"voting_agent\.astream does not support .*\b{param}\b"
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(_collect(agent.astream(judge_message=TemplatedText(content="decide"), **{param: value})))
    with pytest.raises(RuntimeError, match=match):
        asyncio.run(agent.run(judge_message=TemplatedText(content="decide"), **{param: value}))


@pytest.mark.parametrize(("param", "value"), _UNDECLARED_CASES)
def test_both_faces_refuse_an_undeclared_keyword(monkeypatch, app_tools, resource_manager, param, value) -> None:
    _install_reject_fakes(monkeypatch)
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    match = rf"voting_agent\.astream got unknown run keyword argument\(s\): {param}"
    with pytest.raises(TypeError, match=match):
        asyncio.run(_collect(agent.astream(judge_message=TemplatedText(content="decide"), **{param: value})))
    with pytest.raises(TypeError, match=match):
        asyncio.run(agent.run(judge_message=TemplatedText(content="decide"), **{param: value}))


def test_the_not_requested_sentinels_pass_the_guard(monkeypatch, app_tools, resource_manager) -> None:
    _install_reject_fakes(monkeypatch)
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    result = asyncio.run(
        agent.run(
            judge_message=TemplatedText(content="decide"),
            voter_message=TemplatedText(content="answer"),
            thread_id=None,
            user_message=None,
            tool_names=(),
            response_format=None,
        )
    )
    assert isinstance(result, VotingOutput)


def test_a_contract_key_and_a_foreign_response_format_are_named_at_once(
    monkeypatch, app_tools, resource_manager
) -> None:
    _install_reject_fakes(monkeypatch)
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(
            agent.run(
                judge_message=TemplatedText(content="decide"),
                response_format=_NotVotingOutput,  # type: ignore[arg-type]
                thread_id="t",
            )
        )
    assert "response_format" in str(excinfo.value)
    assert "thread_id" in str(excinfo.value)


def test_run_raises_when_required_judge_message_slot_is_unset(monkeypatch, app_tools, resource_manager) -> None:
    """The judge message renders with ``allow_empty=False``: an unset slot (``None``,
    with no alternative like resume) raises loudly rather than silently running on an
    empty prompt. The voter/judge seams are faked so that WITHOUT render.py's
    ``allow_empty`` passthrough the run would instead complete — a dropped passthrough
    turns this red rather than letting the empty prompt slip through."""
    _install_reject_fakes(monkeypatch)
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="required message was not provided"):
        asyncio.run(agent.run(voter_message=TemplatedText(content="answer")))


def test_run_propagates_unknown_template_id_from_the_handle(monkeypatch, app_tools, resource_manager) -> None:
    """A ``judge_message`` naming a stored ``id`` with no registered template surfaces
    the resource manager's unknown-id ``RuntimeError`` (it propagates and aborts the
    run) rather than being silently treated as an empty prompt."""
    _install_reject_fakes(monkeypatch)
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match="unknown template id"):
        asyncio.run(
            agent.run(judge_message=TemplatedText(id="no-such-template"), voter_message=TemplatedText(content="answer"))
        )


def test_voting_agent_input_rejects_unknown_key() -> None:
    """``extra="forbid"`` turns an unknown key on the JSON tool-face into a loud
    validation error rather than a silently ignored field."""
    with pytest.raises(ValidationError):
        VotingAgentInput.model_validate({"judge_message": {"content": "decide"}, "totally_unknown_key": 1})


def test_voting_agent_input_empty_content_kwargs_normalize_to_none() -> None:
    """An empty ``user_content_kwargs`` dict from the JSON door reads as absent — the
    builders treat {} as no mark, so the field normalizes to None rather than a
    set-but-empty value the unhonored-reject face would misread."""
    validated = VotingAgentInput.model_validate({"judge_message": {"content": "decide"}, "user_content_kwargs": {}})
    assert validated.user_content_kwargs is None
    # A non-empty mark is a real value and rides through unchanged.
    marked = VotingAgentInput.model_validate(
        {"judge_message": {"content": "decide"}, "user_content_kwargs": {"cache_control": {"type": "ephemeral"}}}
    )
    assert marked.user_content_kwargs == {"cache_control": {"type": "ephemeral"}}


def test_voter_spec_rejects_unknown_key() -> None:
    """``extra="forbid"`` on the nested ``VoterSpec`` rejects an ``llm_kwargs`` typo
    loudly instead of letting it vanish."""
    with pytest.raises(ValidationError):
        VoterSpec.model_validate({"provider": "p1", "llm_kwarg": {"temperature": 0}})
