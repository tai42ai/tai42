"""``tools_agent`` input model, schema, registration, and the reject-unhonored /
memory-key guard surface.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from pydantic import ValidationError
from tai42_contract.agent import (
    Agent,
)
from tai42_contract.agent.base import PresetSpec, SubAgentSpec
from tai42_contract.app import tai42_app
from tai42_contract.template import TemplatedText
from tests._tools_agent_support import (
    AGENT_NAME,
    _collect,
    _get_agent,
)

from tai42_agents import tools_agent as tools_agent_module
from tai42_agents._internal.reject import reject_unhonored
from tai42_agents.tools_agent import ToolsAgent, ToolsAgentInput


def test_decorator_registers_a_live_instance() -> None:
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    assert isinstance(agent, ToolsAgent)
    assert isinstance(agent, Agent)
    assert agent.tool_name == AGENT_NAME
    assert agent.ToolInput is ToolsAgentInput


def test_tool_input_schema_is_json_able_and_has_no_live_tools_field() -> None:
    """ToolInput carries only JSON-able fields — no live ``tools`` field — so its
    JSON schema builds without a vendor type."""
    schema = ToolsAgentInput.model_json_schema()
    assert "tools" not in schema["properties"]
    assert set(schema["properties"]) >= {"tool_names", "presets", "system_message", "user_message"}


def test_tool_input_parses_presets_as_models() -> None:
    parsed = ToolsAgentInput.model_validate(
        {
            "tool_names": ["search"],
            "presets": [{"name": "my_preset", "base_tool": "example_tool", "fixed_kwargs": {"example_config": {}}}],
        }
    )
    assert parsed.tool_names == ["search"]
    assert parsed.presets is not None
    assert isinstance(parsed.presets[0], PresetSpec)
    assert parsed.presets[0].base_tool == "example_tool"


def test_tool_input_rejects_wrong_typed_field() -> None:
    with pytest.raises(ValidationError):
        ToolsAgentInput.model_validate({"tool_names": "not-a-list"})


def test_spec_runnable_is_true() -> None:
    """``tools_agent`` declares itself authorable, so a deployment that registers it
    has a composable base agent for the authoring UI."""
    assert ToolsAgent.spec_runnable is True


def test_tool_input_advertises_only_the_honored_composable_fields() -> None:
    """``ToolsAgentInput`` advertises exactly the composable spec fields this agent's
    runtime honors — ``system_prompt`` / ``tool_names`` / ``presets`` /
    ``response_format``. The composable fields the runtime cannot honor —
    ``subagents`` / ``strategy`` — are absent from the model and its JSON schema
    entirely (schema-as-capability), not advertised and then rejected at run time."""
    fields = set(ToolsAgentInput.model_fields)
    assert {"subagents", "strategy"}.isdisjoint(fields)
    assert {"system_prompt", "tool_names", "presets", "response_format"} <= fields

    parsed = ToolsAgentInput.model_validate(
        {
            "tool_names": ["search"],
            "presets": [{"name": "p", "base_tool": "example_tool", "fixed_kwargs": {}}],
            "system_prompt": {"content": "you are helpful"},
        }
    )
    assert parsed.presets is not None
    assert isinstance(parsed.presets[0], PresetSpec)
    assert parsed.system_prompt == TemplatedText(content="you are helpful")

    schema = ToolsAgentInput.model_json_schema()
    assert {"system_prompt", "tool_names", "presets", "response_format"} <= set(schema["properties"])
    assert {"subagents", "strategy"}.isdisjoint(schema["properties"])


def test_tool_input_response_format_round_trips() -> None:
    """``response_format`` (a JSON-Schema dict) round-trips through the ToolInput and
    the ``from_tool_input`` mapping — the field feeds the preset/authoring path."""
    schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
    assert "response_format" in ToolsAgentInput.model_json_schema()["properties"]
    validated = ToolsAgentInput.model_validate({"user_message": {"content": "hi"}, "response_format": schema})
    run_kwargs = ToolsAgent.from_tool_input(validated)
    assert run_kwargs["response_format"] == schema


def test_tool_input_rejects_unknown_key() -> None:
    """``extra="forbid"`` turns an unknown key into a loud validation error rather
    than a silently ignored field — including the composable fields this agent drops,
    so a spec that bakes ``strategy`` / ``subagents`` / ``response_format`` fails loud
    instead of having them silently vanish."""
    with pytest.raises(ValidationError):
        ToolsAgentInput.model_validate({"strategy": "vote"})
    with pytest.raises(ValidationError):
        ToolsAgentInput.model_validate({"totally_unknown_key": 1})


def test_empty_content_kwargs_normalize_to_none() -> None:
    """An empty ``user_content_kwargs`` reads as absent — the builders treat {} as no
    mark, so it normalizes to None rather than a set-but-empty value the
    unhonored-reject face would misread. An empty ``system_content_kwargs`` is
    deliberately preserved: {} there is the explicit per-node opt-out from the
    server-wide system-prompt cache default (unset applies the default, {} marks
    nothing)."""
    validated = ToolsAgentInput.model_validate({"system_content_kwargs": {}, "user_content_kwargs": {}})
    assert validated.system_content_kwargs == {}
    assert validated.user_content_kwargs is None
    # A non-empty mark is a real value and rides through unchanged.
    marked = ToolsAgentInput.model_validate({"user_content_kwargs": {"cache_control": {"type": "ephemeral"}}})
    assert marked.user_content_kwargs == {"cache_control": {"type": "ephemeral"}}


def test_from_tool_input_maps_system_prompt_to_system_message() -> None:
    """The ``from_tool_input`` override renames the composable ``system_prompt`` field
    to the ``system_message`` run/astream kwarg (the mapping lives ONLY here), and
    passes every other set field through unchanged — the raw ``system_prompt`` key
    never reaches the run layer."""
    validated = ToolsAgentInput.model_validate({"system_prompt": {"content": "SYS"}, "tool_names": ["search"]})
    run_kwargs = ToolsAgent.from_tool_input(validated)
    assert run_kwargs["system_message"] == TemplatedText(content="SYS")
    assert "system_prompt" not in run_kwargs
    assert run_kwargs["tool_names"] == ["search"]


def test_from_tool_input_without_system_prompt_leaves_kwargs_untouched() -> None:
    """With no ``system_prompt`` set, the override is a pure pass-through of set
    fields (no spurious ``system_message`` key introduced)."""
    validated = ToolsAgentInput.model_validate({"tool_names": ["search"]})
    run_kwargs = ToolsAgent.from_tool_input(validated)
    assert run_kwargs == {"tool_names": ["search"]}


def test_from_tool_input_rejects_both_system_prompt_and_system_message() -> None:
    """``system_prompt`` and ``system_message`` both map to the ``system_message`` run
    kwarg, so setting BOTH is a conflict — rejected loudly rather than silently
    dropping one. This is what stops a run request's ``system_message`` from silently
    overriding an authored agent's baked ``system_prompt``."""
    validated = ToolsAgentInput.model_validate(
        {"system_prompt": {"content": "baked"}, "system_message": {"content": "from-request"}}
    )
    with pytest.raises(ValueError, match="only one of system_prompt or system_message"):
        ToolsAgent.from_tool_input(validated)


# The ABC ``run`` parameters this agent's runtime cannot honor — ALL the guard is
# handed. Each is rejected loudly on BOTH faces rather than silently dropped, so
# dropping any one from the guard call site fails a test here.
# ``response_format`` is NOT here — it is honored (forces structured output).
# ``resume`` is NOT here either — an async ``ask`` park makes a run resumable, so
# a caller-driven ``Command(resume=...)`` is honored (see
# ``test_run_honors_a_caller_driven_resume``).
_UNHONORED_CASES = [
    ("subagents", [SubAgentSpec(name="helper")]),
    ("strategy", "react"),
    ("interrupt_on", {"some_tool": {}}),
    ("skills", ["research"]),
    ("inline_skills", [{"name": "s", "content": "c"}]),
    ("store_provider", "redis"),
]


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_run_rejects_unhonored_params(param: str, value: Any) -> None:
    """``run`` never silently drops an ABC parameter its runtime cannot honor: each
    fails loud with a message naming the offending parameter, on the ``run`` face."""
    agent = _get_agent()
    with pytest.raises(RuntimeError, match=rf"tools_agent\.run does not support .*\b{param}\b"):
        asyncio.run(agent.run(user_message=TemplatedText(content="hi"), **{param: value}))


@pytest.mark.parametrize(("param", "value"), _UNHONORED_CASES)
def test_astream_rejects_unhonored_params(param: str, value: Any) -> None:
    """``astream`` rejects the same unhonored ABC parameters as ``run`` — parity —
    each with a message naming the offending parameter, on the ``astream`` face."""
    agent = _get_agent()
    with pytest.raises(RuntimeError, match=rf"tools_agent\.astream does not support .*\b{param}\b"):
        _collect(agent, user_message=TemplatedText(content="hi"), **{param: value})


def test_unhonored_cases_cover_the_full_reasons_map() -> None:
    """Every key in the guard's reasons map has a parametrized reject case, so a key
    added to the map without a matching test fails here immediately."""
    assert {param for param, _ in _UNHONORED_CASES} == set(tools_agent_module._UNHONORED_REASONS)


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


_COLLECTION_REJECT_PARAMS = sorted(tools_agent_module._UNHONORED_REASONS.keys() & _EMPTY_COLLECTION_ABC_DEFAULTS)


def test_collection_params_match_the_abc_collection_defaults() -> None:
    """``_UNHONORED_COLLECTION_PARAMS`` is exactly this agent's unhonored params whose ABC
    default is an empty collection: no scalar wrongly listed (which would let a meaningful
    falsy value slip through), none dropped (which would over-reject the not-requested
    empty default)."""
    assert set(tools_agent_module._UNHONORED_COLLECTION_PARAMS) == set(_COLLECTION_REJECT_PARAMS)


@pytest.mark.parametrize("empty", [[], ""])
@pytest.mark.parametrize("param", _COLLECTION_REJECT_PARAMS)
def test_reject_unhonored_permits_empty_collection_param(param: str, empty: object) -> None:
    """An empty collection is the ABC's "not requested" default for a collection parameter,
    so the guard does not raise for it — in either falsy empty form (``[]`` / ``""``). Were
    the parameter dropped from ``_UNHONORED_COLLECTION_PARAMS`` it would be classified as a
    scalar (set whenever it is not ``None``) and this empty value would raise."""
    reject_unhonored(
        "tools_agent.run",
        {param: empty},
        tools_agent_module._UNHONORED_REASONS,
        collection_params=tools_agent_module._UNHONORED_COLLECTION_PARAMS,
    )


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("key", ["thread_id", "resume_checkpoint_id"])
def test_astream_rejects_blank_memory_key(key: str, blank: str) -> None:
    """A present-but-blank ``thread_id`` / ``resume_checkpoint_id`` is malformed — it
    names a checkpoint namespace two independent runs would silently share — so
    ``astream`` raises rather than writing it verbatim. Only the keyless default
    ``None`` is the unset path."""
    agent = _get_agent()
    with pytest.raises(ValueError, match=rf"tools_agent\.astream: {key} must be a non-empty string"):
        _collect(agent, user_message=TemplatedText(content="hi"), **{key: blank})


@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("key", ["thread_id", "resume_checkpoint_id"])
def test_run_rejects_blank_memory_key(key: str, blank: str, app_tools: Any, resource_manager: Any) -> None:
    """Parity with ``astream``: ``run`` rejects a present-but-blank memory key loudly
    rather than forwarding it into the run config's ``configurable``."""
    agent = _get_agent()
    with pytest.raises(ValueError, match=rf"tools_agent\.run: {key} must be a non-empty string"):
        # The dynamic key spreads into a typed run parameter, so the arg-type mismatch is expected.
        asyncio.run(agent.run(user_message=TemplatedText(content="hi"), **{key: blank}))  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [123, ["x"]])
@pytest.mark.parametrize("key", ["thread_id", "resume_checkpoint_id"])
def test_astream_rejects_non_string_memory_key(key: str, value: Any) -> None:
    """A non-string ``thread_id`` / ``resume_checkpoint_id`` is a type violation — it
    cannot name a checkpoint namespace — so ``astream`` raises ``TypeError`` naming the
    offending param and the received type rather than probing a non-string for
    whitespace."""
    agent = _get_agent()
    with pytest.raises(
        TypeError,
        match=rf"tools_agent\.astream: {key} must be a string or None; got {type(value).__name__}",
    ):
        _collect(agent, user_message=TemplatedText(content="hi"), **{key: value})


@pytest.mark.parametrize("value", [123, ["x"]])
@pytest.mark.parametrize("key", ["thread_id", "resume_checkpoint_id"])
def test_run_rejects_non_string_memory_key(key: str, value: Any, app_tools: Any, resource_manager: Any) -> None:
    """Parity with ``astream``: ``run`` rejects a non-string memory key with a
    ``TypeError`` naming the offending param and the received type."""
    agent = _get_agent()
    with pytest.raises(
        TypeError,
        match=rf"tools_agent\.run: {key} must be a string or None; got {type(value).__name__}",
    ):
        # The dynamic key spreads into a typed run parameter, so the arg-type mismatch is expected.
        asyncio.run(agent.run(user_message=TemplatedText(content="hi"), **{key: value}))  # type: ignore[arg-type]
