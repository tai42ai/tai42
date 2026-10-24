"""The tool tier bound to a real tool-calling model: single-shot, graph, and the rail.

The tool tier binds a pydantic model and lets LangChain's tool-calling path
(``convert_to_openai_tool`` / ``PydanticToolsParser``) handle it. A dict schema whose
properties are all optional (no ``required``) must convert to an OpenAI tool and bind
cleanly. These tests drive a *real* ``BaseChatModel`` whose default-converting
``bind_tools`` exercises the genuine conversion path, on both doors — the single-shot
``ainvoke_structured`` branch and the graph ``ToolStrategy`` through ``create_agent`` —
and assert:

* no ``TypeError`` is raised binding the optional-field schema;
* the result the consumer receives is a plain ``dict`` conforming to the authored schema;
* an optional property the model omits stays ABSENT, never an explicit ``null`` the authored
  (optional, non-nullable) schema would reject.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any, cast

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, PrivateAttr
from tai42_kit.llm.structured import plan_structured_output
from tai42_kit.utils.data.json_schema_util import inject_int64_bounds, json_schema_to_pydantic_model

from tai42_agents._internal import structured as structured_mod
from tai42_agents._internal.outcomes import drive_reprompt_scope
from tai42_agents._internal.structured import ainvoke_structured, structured_output_stack
from tai42_agents._internal.structured_rail import StructuredOutputRailMiddleware

_OPTIONAL_SCHEMA = {"title": "Opt", "type": "object", "properties": {"s": {"type": "string"}}}
# A schema whose property names are a python keyword (``from``) and a builtin shadow
# (``id``): the generated bound model sanitizes them to legal field names and keeps the
# authored key as each field's alias. The value the consumer receives must carry the
# AUTHORED keys, never the sanitized field names.
_KEYWORD_SCHEMA = {
    "title": "Kw",
    "type": "object",
    "properties": {"from": {"type": "string"}, "id": {"type": "string"}},
    "required": ["from", "id"],
}
_NESTED_OPTIONAL_SCHEMA = {
    "title": "Nested",
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {"x": {"type": "integer"}}}}},
}


class _Payload(BaseModel):
    """A pydantic-class response_format with an optional field carrying a non-None default."""

    value: int
    note: str = "default"


class _ToolCallModel(BaseChatModel):
    """A real chat model whose default-converting ``bind_tools`` runs the genuine
    ``convert_to_openai_tool`` path, answering each forced structured tool call with the
    next scripted args — the shape that crashed when the tool tier bound a TypedDict."""

    _args_seq: list[dict[str, Any]] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)
    _bound: list[list[dict[str, Any]]] = PrivateAttr(default_factory=list)

    def __init__(self, args_seq: Sequence[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._args_seq = list(args_seq)

    @property
    def _llm_type(self) -> str:
        return "tool-call"

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        formatted = [convert_to_openai_tool(tool) for tool in tools]
        self._bound.append(formatted)
        return self.bind(tools=formatted, tool_choice=tool_choice, **kwargs)

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        tools = kwargs.get("tools") or []
        name = tools[0]["function"]["name"] if tools else "Response"
        args = self._args_seq[min(self._index, len(self._args_seq) - 1)]
        self._index += 1
        message = AIMessage(content="", tool_calls=[{"id": "c", "name": name, "args": args}])
        return ChatResult(generations=[ChatGeneration(message=message)])


def _cap(monkeypatch: pytest.MonkeyPatch, cap: int) -> None:
    monkeypatch.setattr(
        structured_mod, "agents_limits_settings", lambda: SimpleNamespace(structured_output_reprompt_cap=cap)
    )


# --- single-shot route: ainvoke_structured tool branch -------------------------


def _single_shot(model: _ToolCallModel, schema: dict[str, Any] | type[BaseModel]) -> Any:
    plan = plan_structured_output(cast(BaseChatModel, model), "openai", schema)
    assert plan.mode == "tool"
    return asyncio.run(ainvoke_structured(cast(BaseChatModel, model), plan, [HumanMessage(content="go")]))


def test_single_shot_optional_field_binds_and_returns_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    _cap(monkeypatch, 3)
    out = _single_shot(_ToolCallModel([{"s": "hi"}]), _OPTIONAL_SCHEMA)
    assert out == {"s": "hi"}
    assert isinstance(out, dict)


def test_single_shot_omitted_optional_stays_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _cap(monkeypatch, 3)
    out = _single_shot(_ToolCallModel([{}]), _OPTIONAL_SCHEMA)
    assert out == {}


def test_single_shot_keyword_property_round_trips_to_the_authored_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _cap(monkeypatch, 3)
    out = _single_shot(_ToolCallModel([{"from": "A", "id": "X"}]), _KEYWORD_SCHEMA)
    assert out == {"from": "A", "id": "X"}


def test_single_shot_nested_optional_array(monkeypatch: pytest.MonkeyPatch) -> None:
    _cap(monkeypatch, 3)
    out = _single_shot(_ToolCallModel([{"items": [{"x": 1}, {}]}]), _NESTED_OPTIONAL_SCHEMA)
    assert out == {"items": [{"x": 1}, {}]}


def test_single_shot_pydantic_class_yields_an_instance_with_omitted_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pydantic-class response_format keeps its class: the tool tier parses the bound
    # (generated) model and the validator re-inflates the authored class, the model-omitted
    # optional falling back to the class default — never a stray ``null``.
    _cap(monkeypatch, 3)
    out = _single_shot(_ToolCallModel([{"value": 7}]), _Payload)
    assert isinstance(out, _Payload)
    assert out.value == 7
    assert out.note == "default"


# --- graph route: ToolStrategy through create_agent + the rail -----------------


def _run_graph(
    monkeypatch: pytest.MonkeyPatch, model: _ToolCallModel, schema: dict[str, Any] | type[BaseModel]
) -> tuple[Any, Any]:
    _cap(monkeypatch, 3)
    strategy, rail = structured_output_stack(cast(BaseChatModel, model), "openai", schema)
    agent = create_agent(model, tools=[], response_format=strategy, middleware=[rail] if rail else [])

    async def drive() -> Any:
        # The test is the graph's driver, so it binds the drive's re-prompt counters.
        with drive_reprompt_scope():
            return await agent.ainvoke({"messages": [HumanMessage(content="go")]})

    out = asyncio.run(drive())
    return out, strategy


def test_graph_optional_field_binds_the_authored_dict_and_yields_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    out, strategy = _run_graph(monkeypatch, _ToolCallModel([{"s": "hi"}]), _OPTIONAL_SCHEMA)
    assert isinstance(strategy, ToolStrategy)
    assert strategy.schema == _OPTIONAL_SCHEMA
    response = out["structured_response"]
    assert response == {"s": "hi"}
    assert isinstance(response, dict)


def test_graph_keyword_property_round_trips_to_the_authored_key(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _strategy = _run_graph(monkeypatch, _ToolCallModel([{"from": "A", "id": "X"}]), _KEYWORD_SCHEMA)
    assert out["structured_response"] == {"from": "A", "id": "X"}


def test_graph_omitted_optional_stays_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _strategy = _run_graph(monkeypatch, _ToolCallModel([{}]), _OPTIONAL_SCHEMA)
    assert out["structured_response"] == {}


def test_graph_pydantic_class_yields_an_instance_with_omitted_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pydantic-class response_format through the graph tool tier stores an instance of the
    # authored class (not the generated bound model, nor a bare dict), the model-omitted
    # optional falling back to the class default.
    out, strategy = _run_graph(monkeypatch, _ToolCallModel([{"value": 7}]), _Payload)
    assert isinstance(strategy, ToolStrategy)
    response = out["structured_response"]
    assert isinstance(response, _Payload)
    assert response.value == 7
    assert response.note == "default"


# --- the rail normalizes a pydantic structured_response to a dict --------------


async def _run_rail_success(rail: StructuredOutputRailMiddleware, response: ModelResponse) -> ModelResponse:
    async def handler(_request: ModelRequest) -> ModelResponse:
        return response

    # The success path does not read the request, so a bare object stands in for it.
    with drive_reprompt_scope():
        return await rail.awrap_model_call(cast(ModelRequest, object()), handler)


def test_rail_normalizes_a_pydantic_structured_response_to_a_dict() -> None:
    model_cls = json_schema_to_pydantic_model(inject_int64_bounds(_OPTIONAL_SCHEMA), model_name="Opt")
    rail = StructuredOutputRailMiddleware(_OPTIONAL_SCHEMA, cap=3)

    present = ModelResponse(result=[AIMessage(content="")], structured_response=model_cls.model_validate({"s": "hi"}))
    out = asyncio.run(_run_rail_success(rail, present))
    assert out.structured_response == {"s": "hi"}
    assert isinstance(out.structured_response, dict)

    omitted = ModelResponse(result=[AIMessage(content="")], structured_response=model_cls.model_validate({}))
    out2 = asyncio.run(_run_rail_success(rail, omitted))
    assert out2.structured_response == {}


# --- the authored schema is what both doors bind -------------------------------

_INT_SCHEMA = {"title": "Count", "type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}


def test_single_shot_keeps_an_extra_key_the_schema_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    _cap(monkeypatch, 3)
    out = _single_shot(_ToolCallModel([{"value": 1, "note": "extra"}]), _INT_SCHEMA)
    assert out == {"value": 1, "note": "extra"}


def test_graph_keeps_an_extra_key_the_schema_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    out, _strategy = _run_graph(monkeypatch, _ToolCallModel([{"value": 1, "note": "extra"}]), _INT_SCHEMA)
    assert out["structured_response"] == {"value": 1, "note": "extra"}


def test_single_shot_reprompts_a_string_for_an_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    _cap(monkeypatch, 3)
    model = _ToolCallModel([{"value": "7"}, {"value": 7}])
    out = _single_shot(model, _INT_SCHEMA)
    assert out == {"value": 7}
    assert model._index == 2


def test_graph_reprompts_a_string_for_an_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _ToolCallModel([{"value": "7"}, {"value": 7}])
    out, _strategy = _run_graph(monkeypatch, model, _INT_SCHEMA)
    assert out["structured_response"] == {"value": 7}
    assert model._index == 2


def test_both_doors_bind_the_same_parameters(monkeypatch: pytest.MonkeyPatch) -> None:
    authored = {
        "title": "Rich",
        "type": "object",
        "properties": {
            "count": {"type": "integer", "description": "how many"},
            "label": {"type": "string", "default": "none"},
        },
        "required": ["count"],
    }
    single = _ToolCallModel([{"count": 1}])
    _cap(monkeypatch, 3)
    _single_shot(single, authored)
    graph = _ToolCallModel([{"count": 1}])
    _run_graph(monkeypatch, graph, authored)

    single_params = single._bound[0][0]["function"]["parameters"]
    graph_params = graph._bound[0][0]["function"]["parameters"]
    assert single_params == graph_params
    # The authored schema reaches the model: no int64 bounds, the description and default kept.
    assert single_params["properties"]["count"] == {"type": "integer", "description": "how many"}
    assert single_params["properties"]["label"]["default"] == "none"


def test_single_shot_refuses_a_fan_out_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_kit.llm.structured import StructuredOutputUnsupportedError

    schema = {
        "title": "Either",
        "oneOf": [
            {"title": "Left", "type": "object", "properties": {"l": {"type": "string"}}},
            {"title": "Right", "type": "object", "properties": {"r": {"type": "integer"}}},
        ],
    }
    _cap(monkeypatch, 3)
    with pytest.raises(StructuredOutputUnsupportedError, match="single-call door binds one object schema"):
        _single_shot(_ToolCallModel([{"l": "x"}]), schema)


# --- single-shot route: a turn with no structured tool call --------------------


class _TurnsModel(BaseChatModel):
    """A real tool-calling chat model answering each call with the next scripted turn.

    A ``dict`` turn is the structured tool call carrying those args; a ``str`` turn is a
    message-text answer with no tool call. Each call's messages are recorded.
    """

    _turns: list[dict[str, Any] | str] = PrivateAttr(default_factory=list)
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, turns: Sequence[dict[str, Any] | str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._turns = list(turns)

    @property
    def _llm_type(self) -> str:
        return "turns"

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], tool_choice=tool_choice, **kwargs)

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        turn = self._turns[min(len(self._seen), len(self._turns) - 1)]
        self._seen.append(list(messages))
        if isinstance(turn, str):
            message = AIMessage(content=turn)
        else:
            name = kwargs["tools"][0]["function"]["name"]
            message = AIMessage(content="", tool_calls=[{"id": "c", "name": name, "args": turn}])
        return ChatResult(generations=[ChatGeneration(message=message)])


def test_single_shot_text_answer_is_reprompted_as_a_missing_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    _cap(monkeypatch, 3)
    model = _TurnsModel(['{"value": 7}', {"value": 7}])

    out = _single_shot(cast(_ToolCallModel, model), _INT_SCHEMA)

    assert out == {"value": 7}
    assert len(model._seen) == 2
    feedback = model._seen[1][-1]
    assert isinstance(feedback, HumanMessage)
    assert "'Count'" in str(feedback.content)


def test_single_shot_never_answering_ends_on_the_cap_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_agents._internal.outcomes import RepromptCapError

    _cap(monkeypatch, 3)
    model = _TurnsModel([""])

    with pytest.raises(RepromptCapError) as caught:
        _single_shot(cast(_ToolCallModel, model), _INT_SCHEMA)

    assert caught.value.attempts == 4
    assert len(model._seen) == 4
    assert "'Count'" in caught.value.error
