"""``refine_agent`` contract surface: reject-unhonored guards, the required
evaluator-message slot, and the input model.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from langchain_core.messages import AIMessageChunk
from pydantic import ValidationError
from tai42_contract.agent import (
    Agent,
    MessageFinal,
)
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._refine_agent_support import (
    AGENT_NAME,
    FakeAgent,
    _a_tool,
    _collect,
    _patch_loop,
)

import tai42_agents.refine_agent.agent as agent_mod
from tai42_agents._internal.reject import reject_unhonored
from tai42_agents.refine_agent.agent import RefineAgentInput
from tai42_agents.refine_agent.prompt import (
    CRITIC_APPROVAL_MESSAGE,
)


def test_tool_names_honored_on_run_face(monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any) -> None:
    """The ``run`` face resolves ``tool_names`` and compiles both agents with the
    live tool — parity with the ``astream`` face, not a silent drop."""
    tool = _a_tool()
    app_tools.client_tools["t1"] = tool
    evaluator = FakeAgent(
        invoke_contents=["draft"],
        stream_items=[("messages", (AIMessageChunk(content="ok"), {}))],
    )
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    recorder = _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    asyncio.run(
        agent.run(
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            tool_names=["t1"],
        )
    )

    # Both compiled with the live tool — by SURFACE, not object identity (each pass gets a
    # delivery-scoped copy), so this is parity with the astream face, not a silent drop.
    expected = [(tool.name, tool.description, tool.args)]
    assert [[(t.name, t.description, t.args) for t in call] for call in recorder.tools_per_call] == [
        expected,
        expected,
    ]


# Each unhonored contract parameter with a MEANINGFUL value — a truthy collection or
# a not-``None`` scalar — plus two falsy-but-meaningful scalars (``strategy=""``,
# ``resume=False``) that must still raise (they are set whenever not ``None``, so they
# never slip through a truthiness gate). ``X=None`` / ``X=()`` are the ABC's own
# "not requested" sentinels and are pinned to PASS by the tests below.
_UNHONORED_CASES = [
    ("tools", [object()]),
    ("presets", [object()]),
    ("subagents", [object()]),
    ("strategy", "react"),
    ("strategy", ""),
    ("system_message", "sys"),
    ("user_message", "usr"),
    ("interrupt_on", {"tool": True}),
    ("skills", ["s"]),
    ("inline_skills", [{"name": "s", "content": "c"}]),
    ("recursion_limit", 0),
    ("thread_id", "t"),
    ("resume", False),
    ("resume_checkpoint_id", "cp"),
    ("llm_provider", "openai"),
    ("store_provider", "redis"),
    ("llm_kwargs", {"model": "x"}),
    ("system_content_kwargs", {"cache_control": {"type": "ephemeral"}}),
]


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_astream_rejects_unhonored_contract_param(
    param: str, value: Any, app_tools: Any, resource_manager: Any
) -> None:
    """Every contract ``Agent.run`` parameter with no seat in the role-named loop
    raises loudly on the stream face, naming the offending parameter and the exact
    ``astream`` face it was called on. Falsy-but-meaningful scalars (``strategy=""``,
    ``resume=False``) still raise."""
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=rf"refine_agent\.astream does not support .*\b{param}\b"):
        _collect(
            agent,
            evaluator_message=TemplatedText(content="write it"),
            critic_message=TemplatedText(content="review it"),
            **{param: value},
        )


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_run_rejects_unhonored_contract_param(param: str, value: Any, app_tools: Any, resource_manager: Any) -> None:
    """The ``run`` face rejects the same unsupported parameters, naming the exact
    ``run`` face — the run-face guard is load-bearing (were it dropped, the delegated
    ``astream`` guard would surface the ``astream`` token and fail this assertion)."""
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(RuntimeError, match=rf"refine_agent\.run does not support .*\b{param}\b"):
        # The base ``Agent.run`` signature types some of these non-optional, so the mismatch is expected.
        asyncio.run(
            agent.run(
                evaluator_message=TemplatedText(content="write it"),
                critic_message=TemplatedText(content="review it"),
                **{param: value},
            )
        )  # type: ignore[arg-type]


def test_unhonored_cases_cover_the_full_reasons_map() -> None:
    """Every key in the guard's reasons map has a parametrized reject case, so a key
    added to the map without a matching test fails here immediately."""
    assert {param for param, _ in _UNHONORED_CASES} == set(agent_mod._UNHONORED_REASONS)


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


_COLLECTION_REJECT_PARAMS = sorted(agent_mod._UNHONORED_REASONS.keys() & _EMPTY_COLLECTION_ABC_DEFAULTS)


def test_collection_params_match_the_abc_collection_defaults() -> None:
    """``_UNHONORED_COLLECTION_PARAMS`` is exactly this agent's unhonored params whose ABC
    default is an empty collection: no scalar wrongly listed (which would let a meaningful
    falsy value slip through), none dropped (which would over-reject the not-requested
    empty default)."""
    assert set(agent_mod._UNHONORED_COLLECTION_PARAMS) == set(_COLLECTION_REJECT_PARAMS)


@pytest.mark.parametrize("empty", [[], ""])
@pytest.mark.parametrize("param", _COLLECTION_REJECT_PARAMS)
def test_reject_unhonored_permits_empty_collection_param(param: str, empty: object) -> None:
    """An empty collection is the ABC's "not requested" default for a collection parameter,
    so the guard does not raise for it — in either falsy empty form (``[]`` / ``""``). Were
    the parameter dropped from ``_UNHONORED_COLLECTION_PARAMS`` it would be classified as a
    scalar (set whenever it is not ``None``) and this empty value would raise."""
    reject_unhonored(
        "refine_agent.run",
        {param: empty},
        agent_mod._UNHONORED_REASONS,
        collection_params=agent_mod._UNHONORED_COLLECTION_PARAMS,
    )


@pytest.mark.parametrize(
    "param", ["response_format", "strategy", "thread_id", "resume", "llm_provider", "recursion_limit"]
)
def test_unhonored_scalar_none_passes_the_guard(param: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ABC's ``None`` "not requested" sentinel passes the guard rather than being
    over-rejected: an explicit ``response_format=None`` / ``resume=None`` / … reaches
    the loop and completes, in parity with the LangGraph-backed agents."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(
        agent,
        evaluator_message=TemplatedText(content="write it"),
        critic_message=TemplatedText(content="review it"),
        **{param: None},
    )
    assert any(isinstance(e, MessageFinal) for e in events)


def test_unhonored_collection_empty_passes_the_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty collection is the ABC's "not requested" sentinel and passes the guard:
    ``tools=()`` / ``presets=[]`` reach the loop rather than raising."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(
        agent,
        evaluator_message=TemplatedText(content="write it"),
        critic_message=TemplatedText(content="review it"),
        tools=(),
        presets=[],
    )
    assert any(isinstance(e, MessageFinal) for e in events)


def test_extension_kwarg_is_not_rejected(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """A plain extension kwarg beyond the contract names is the documented
    extension point and passes through untouched rather than raising."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    events = _collect(
        agent,
        evaluator_message=TemplatedText(content="write it"),
        critic_message=TemplatedText(content="review it"),
        role_specific_extra="ok",
    )

    assert any(isinstance(e, MessageFinal) for e in events)


def test_run_raises_when_required_evaluator_message_slot_is_unset(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any, resource_manager: Any
) -> None:
    """The evaluator message renders with ``allow_empty=False``: an unset slot
    (``None``) raises loudly rather than silently running the loop on an empty prompt.
    The evaluator/critic seams are faked so that WITHOUT render.py's ``allow_empty``
    passthrough the loop would instead run to approval — a dropped passthrough turns
    this red rather than letting the empty prompt slip through."""
    evaluator = FakeAgent(invoke_contents=["draft"], stream_items=[("messages", (AIMessageChunk(content="ok"), {}))])
    critic = FakeAgent(invoke_contents=[f"approved {CRITIC_APPROVAL_MESSAGE}"])
    _patch_loop(monkeypatch, [evaluator, critic])

    agent = tai42_app.agents.get_agent(AGENT_NAME)
    with pytest.raises(ValueError, match="required message was not provided"):
        asyncio.run(agent.run(critic_message=TemplatedText(content="review it")))


def test_tool_input_rejects_unknown_key() -> None:
    """A typo'd key is rejected loudly at validation rather than silently ignored."""
    with pytest.raises(ValidationError, match="max_iteration"):
        RefineAgentInput.model_validate({"evaluator_message": {"content": "hi"}, "max_iteration": 5})


def test_empty_content_kwargs_normalize_to_none() -> None:
    """An empty ``user_content_kwargs`` dict from the JSON door reads as absent — the
    builders treat {} as no mark, so the field normalizes to None rather than a
    set-but-empty value the unhonored-reject face would misread."""
    validated = RefineAgentInput.model_validate({"evaluator_message": {"content": "hi"}, "user_content_kwargs": {}})
    assert validated.user_content_kwargs is None
    # A non-empty mark is a real value and rides through unchanged.
    marked = RefineAgentInput.model_validate(
        {"evaluator_message": {"content": "hi"}, "user_content_kwargs": {"cache_control": {"type": "ephemeral"}}}
    )
    assert marked.user_content_kwargs == {"cache_control": {"type": "ephemeral"}}
