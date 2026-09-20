"""``voting_agent`` contract surface: response-format acceptance/rejection,
reject-unhonored guards, and the input/model validation.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.agent import Agent
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
    _make_tool,
    _NotVotingOutput,
)

from tai42_agents._internal.reject import reject_unhonored
from tai42_agents.voting_agent import agent as agent_module
from tai42_agents.voting_agent.agent import VotingAgentInput
from tai42_agents.voting_agent.model import VoterSpec, VotingOutput


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
            response_format=VotingOutput,
        )
    )
    assert isinstance(result, VotingOutput)


def test_astream_rejects_foreign_response_format(monkeypatch, app_tools, resource_manager) -> None:
    """A ``response_format`` other than ``VotingOutput`` is rejected loudly on the
    stream face rather than silently ignored."""
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=r"voting_agent\.astream does not support response_format"):
        asyncio.run(
            _collect(agent.astream(judge_message=TemplatedText(content="decide"), response_format=_NotVotingOutput))
        )


def test_run_rejects_foreign_response_format(monkeypatch, app_tools, resource_manager) -> None:
    """A ``response_format`` other than ``VotingOutput`` is rejected loudly on the
    run face rather than silently replaced by the constant drain format. The run-face
    guard is load-bearing: dropping it would surface the ``astream`` token here."""
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=r"voting_agent\.run does not support response_format"):
        asyncio.run(agent.run(judge_message=TemplatedText(content="decide"), response_format=_NotVotingOutput))


def test_astream_rejects_unhonored_abc_param(monkeypatch, app_tools, resource_manager) -> None:
    """An ABC ``Agent.run`` param the voting runtime has no seat for (``thread_id``)
    is rejected loudly on the stream face, naming it and the exact ``astream`` face."""
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=r"voting_agent\.astream does not support .*\bthread_id\b"):
        asyncio.run(_collect(agent.astream(judge_message=TemplatedText(content="decide"), thread_id="t1")))


def test_run_rejects_unhonored_abc_param(monkeypatch, app_tools, resource_manager) -> None:
    """An ABC ``Agent.run`` param the voting runtime has no seat for (``tools``) is
    rejected loudly on the run face, naming it and the exact ``run`` face. The
    run-face guard is load-bearing: dropping it would surface the ``astream`` token."""
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=r"voting_agent\.run does not support .*\btools\b"):
        asyncio.run(agent.run(judge_message=TemplatedText(content="decide"), tools=[_make_tool("x")]))


@pytest.mark.parametrize(("param", "value"), [("strategy", ""), ("resume", False), ("recursion_limit", 0)])
def test_run_rejects_falsy_but_meaningful_abc_param(monkeypatch, app_tools, resource_manager, param, value) -> None:
    """A falsy-but-meaningful scalar (``strategy=""`` / ``resume=False`` /
    ``recursion_limit=0``) the voting runtime has no seat for still raises — a scalar
    is set whenever it is not ``None``, so it never slips through a truthiness gate."""
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=rf"voting_agent\.run does not support .*\b{param}\b"):
        # The base Agent.run signature types some of these non-optional, so the mismatch is expected.
        asyncio.run(
            agent.run(
                judge_message=TemplatedText(content="decide"),
                voter_message=TemplatedText(content="answer"),
                **{param: value},
            )
        )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "param", ["thread_id", "resume", "recursion_limit", "strategy", "response_format", "llm_provider"]
)
def test_unhonored_none_passes_the_guard(monkeypatch, app_tools, resource_manager, param) -> None:
    """The ABC's ``None`` "not requested" sentinel passes the guard rather than being
    over-rejected: an explicit ``thread_id=None`` / ``response_format=None`` / … reaches
    the voting flow and produces the terminal ``VotingOutput``, in parity with the other
    agents. (``response_format=None`` is not the foreign-type case, which still raises.)"""
    monkeypatch.setattr(agent_module, "ainvoke_tools_agent", _fake_voter_invoke([]))
    monkeypatch.setattr(agent_module, "astream_tools_agent_events", _fake_full_judge_stream({}))

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    # The base Agent.run signature types some of these non-optional, so the None mismatch is expected.
    result = asyncio.run(
        agent.run(
            judge_message=TemplatedText(content="decide"),
            voter_message=TemplatedText(content="answer"),
            **{param: None},  # type: ignore[arg-type]
        )
    )
    assert isinstance(result, VotingOutput)


# Every key in ``_UNHONORED_REASONS`` paired with a representative SET value.
# ``response_format`` folds into the same guard: a foreign type is an offender.
# ``resume=False`` pins the falsy-but-present scalar case.
_UNHONORED_CASES = [
    ("tools", [_make_tool("x")]),
    ("tool_names", ["x"]),
    ("presets", [object()]),
    ("subagents", [object()]),
    ("system_message", "sys"),
    ("user_message", "usr"),
    ("strategy", "vote"),
    ("interrupt_on", {"tool": True}),
    ("skills", ["s"]),
    ("inline_skills", [{"name": "s", "content": "c"}]),
    ("recursion_limit", 5),
    ("thread_id", "t1"),
    ("resume", False),
    ("resume_checkpoint_id", "cp"),
    ("llm_provider", "openai"),
    ("store_provider", "redis"),
    ("llm_kwargs", {"model": "x"}),
    ("response_format", _NotVotingOutput),
    ("system_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
]


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_run_rejects_every_unhonored_param(monkeypatch, app_tools, resource_manager, param, value) -> None:
    """run rejects every key in the guard's reasons map, naming it and the run face."""
    _install_reject_fakes(monkeypatch)
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=rf"voting_agent\.run does not support .*\b{param}\b"):
        asyncio.run(agent.run(judge_message=TemplatedText(content="decide"), **{param: value}))


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_astream_rejects_every_unhonored_param(monkeypatch, app_tools, resource_manager, param, value) -> None:
    """astream rejects the same full set as run — parity — naming the astream face."""
    _install_reject_fakes(monkeypatch)
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=rf"voting_agent\.astream does not support .*\b{param}\b"):
        asyncio.run(_collect(agent.astream(judge_message=TemplatedText(content="decide"), **{param: value})))


def test_unhonored_cases_cover_the_full_reasons_map() -> None:
    """Every key in the reasons map has a parametrized reject case; a key added without
    a test fails here immediately."""
    assert {param for param, _ in _UNHONORED_CASES} == set(agent_module._UNHONORED_REASONS)


# The unhonored params whose ABC ``Agent.run`` default is an empty collection (``()`` /
# ``""``), read from the contract signature — the independent source of truth for which
# unhonored params are collection-typed. Intersected with this agent's reasons map it is
# exactly the set ``_UNHONORED_COLLECTION_PARAMS`` must classify as collections. The
# empty-collection test below parametrizes from HERE, not from the frozenset, so a member
# dropped from the frozenset (reclassifying it as a scalar) turns a case red rather than
# silently vanishing.
_EMPTY_COLLECTION_ABC_DEFAULTS = frozenset(
    name
    for name, parameter in inspect.signature(Agent.run).parameters.items()
    if isinstance(parameter.default, (tuple, list, str)) and not parameter.default
)


_COLLECTION_REJECT_PARAMS = sorted(agent_module._UNHONORED_REASONS.keys() & _EMPTY_COLLECTION_ABC_DEFAULTS)


def test_collection_params_match_the_abc_collection_defaults() -> None:
    """``_UNHONORED_COLLECTION_PARAMS`` is exactly this agent's unhonored params whose ABC
    default is an empty collection: no scalar wrongly listed (which would let a meaningful
    falsy value slip through), none dropped (which would over-reject the not-requested
    empty default)."""
    assert set(agent_module._UNHONORED_COLLECTION_PARAMS) == set(_COLLECTION_REJECT_PARAMS)


@pytest.mark.parametrize("empty", [[], ""])
@pytest.mark.parametrize("param", _COLLECTION_REJECT_PARAMS)
def test_reject_unhonored_permits_empty_collection_param(param: str, empty: object) -> None:
    """An empty collection is the ABC's "not requested" default for a collection parameter,
    so the guard does not raise for it — in either falsy empty form (``[]`` / ``""``). Were
    the parameter dropped from ``_UNHONORED_COLLECTION_PARAMS`` it would be classified as a
    scalar (set whenever it is not ``None``) and this empty value would raise."""
    reject_unhonored(
        "voting_agent.run",
        {param: empty},
        agent_module._UNHONORED_REASONS,
        collection_params=agent_module._UNHONORED_COLLECTION_PARAMS,
    )


def test_run_names_response_format_and_another_offender_at_once(monkeypatch, app_tools, resource_manager) -> None:
    """A foreign ``response_format`` AND another unhonored param are named in ONE raise:
    the response_format check folds into the shared guard, so the caller fixes both in a
    single pass rather than one raise per run."""
    _install_reject_fakes(monkeypatch)
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError) as excinfo:
        # subagents is typed Sequence[SubAgentSpec]; the object() is deliberately wrong to exercise the guard.
        asyncio.run(
            agent.run(
                judge_message=TemplatedText(content="decide"),
                response_format=_NotVotingOutput,
                subagents=[object()],  # type: ignore[list-item]
            )
        )
    message = str(excinfo.value)
    assert "response_format" in message
    assert "subagents" in message


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
