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

    def __init__(self, args_seq: Sequence[dict[str, Any]], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._args_seq = list(args_seq)

    @property
    def _llm_type(self) -> str:
        return "tool-call"

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        formatted = [convert_to_openai_tool(tool) for tool in tools]
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


def test_graph_optional_field_binds_a_pydantic_model_and_yields_a_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    out, strategy = _run_graph(monkeypatch, _ToolCallModel([{"s": "hi"}]), _OPTIONAL_SCHEMA)
    assert isinstance(strategy, ToolStrategy)
    assert isinstance(strategy.schema, type)
    assert issubclass(strategy.schema, BaseModel)
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


# --- the shown contract equals the enforced contract --------------------------


def _shown_properties(model: _ToolCallModel, schema: dict[str, Any]) -> dict[str, Any]:
    strategy, _rail = structured_output_stack(cast(BaseChatModel, model), "openai", schema)
    tool = convert_to_openai_tool(strategy.schema)
    return tool["function"]["parameters"]["properties"]


def test_shown_optional_property_has_no_null_member_or_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _cap(monkeypatch, 3)
    # The schema the model is SHOWN for an optional, non-nullable property must carry the
    # bare type — no ``null`` member, no ``default`` inviting a null — so it matches the
    # authored schema the rail enforces.
    shown = _shown_properties(_ToolCallModel([{}]), _OPTIONAL_SCHEMA)
    assert "anyOf" not in shown["s"]
    assert "default" not in shown["s"]
    assert shown["s"]["type"] == "string"


@pytest.mark.parametrize(
    ("payload", "accepted"),
    [({"s": "hi"}, True), ({}, True), ({"s": None}, False), ({"s": 7}, False)],
)
def test_shown_contract_matches_the_rail_verdict(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any], accepted: bool
) -> None:
    # For each payload, acceptance under the SHOWN tool schema equals the rail's verdict:
    # a null the rendering would invite is exactly a null the authored schema refuses.
    import jsonschema

    _cap(monkeypatch, 3)
    strategy, _rail = structured_output_stack(cast(BaseChatModel, _ToolCallModel([{}])), "openai", _OPTIONAL_SCHEMA)
    shown_schema = convert_to_openai_tool(strategy.schema)["function"]["parameters"]
    shown_ok = next(iter(jsonschema.Draft202012Validator(shown_schema).iter_errors(payload)), None) is None
    authored_ok = next(iter(jsonschema.Draft202012Validator(_OPTIONAL_SCHEMA).iter_errors(payload)), None) is None
    assert shown_ok == authored_ok == accepted


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
