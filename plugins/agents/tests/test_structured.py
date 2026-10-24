"""``structured_output_stack``: minting the strategy + rail for a compiled run.

A model that declares native structured output over a bindable, representable schema
yields a :class:`NativeStrategy` whose ``to_model_kwargs()`` is the kit's provider-native
binding; a model that declares none (or a non-representable schema) yields a
``ToolStrategy`` over the int64-bounded pydantic model with ``handle_errors is False`` (the
platform rail owns the retry). Both come paired with the per-run re-prompt rail. ``None``
(no structured output) and an explicit ``ToolStrategy``/``ProviderStrategy`` pass through
untouched, with no rail.
"""

from __future__ import annotations

from typing import Any, cast

from langchain.agents.structured_output import AutoStrategy, ProviderStrategy, ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, TypeAdapter
from tai42_kit.llm.models import native_structured_output_kwargs
from tai42_kit.utils.data.json_schema_util import to_portable_schema

from tai42_agents._internal.structured import NativeStrategy, structured_output_stack
from tai42_agents._internal.structured_rail import StructuredOutputRailMiddleware

_SCHEMA = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}

INT64_MAX = 9223372036854775807


class _Model(BaseModel):
    value: int


class _Node(BaseModel):
    next: _Node | None = None


_Node.model_rebuild()


class _Fake:
    """A duck-typed chat model exposing only ``profile``."""

    def __init__(self, profile: dict[str, Any] | None) -> None:
        self.profile = profile
        self.model_name = "fake-model"


def _llm(profile: dict[str, Any] | None) -> BaseChatModel:
    return cast(BaseChatModel, _Fake(profile))


def test_none_passes_through_without_a_rail() -> None:
    strategy, rail = structured_output_stack(_llm({"structured_output": True}), "openai", None)
    assert strategy is None
    assert rail is None


def test_explicit_tool_strategy_binds_its_schemas_with_the_rail_as_the_judge() -> None:
    tool = ToolStrategy(_SCHEMA, tool_message_content="stored")
    strategy, rail = structured_output_stack(_llm(None), "openai", tool)
    assert isinstance(strategy, ToolStrategy)
    assert strategy.handle_errors is False
    assert strategy.schema == _SCHEMA
    assert strategy.schema_specs == tool.schema_specs
    assert strategy.tool_message_content == "stored"
    assert isinstance(rail, StructuredOutputRailMiddleware)
    # The caller's strategy is left as it was.
    assert tool.handle_errors is True


def test_explicit_provider_strategy_keeps_its_binding_and_gets_a_rail() -> None:
    provider = ProviderStrategy(_Model)
    strategy, rail = structured_output_stack(_llm(None), "openai", provider)
    assert strategy is provider
    assert isinstance(rail, StructuredOutputRailMiddleware)


def test_native_strategy_carries_the_kit_kwargs_and_a_rail() -> None:
    strategy, rail = structured_output_stack(_llm({"structured_output": True}), "openai", _SCHEMA)
    assert isinstance(strategy, NativeStrategy)
    assert strategy.to_model_kwargs() == native_structured_output_kwargs(
        "openai", "Answer", to_portable_schema(_SCHEMA)
    )
    assert isinstance(rail, StructuredOutputRailMiddleware)


def test_tool_strategy_over_the_authored_dict_with_handle_errors_false() -> None:
    strategy, rail = structured_output_stack(_llm({}), "openai", _SCHEMA)
    assert isinstance(strategy, ToolStrategy)
    assert strategy.handle_errors is False
    assert isinstance(rail, StructuredOutputRailMiddleware)
    # The tool tier binds the authored dict; the rail is the single judge of the value.
    assert strategy.schema == _SCHEMA


def test_auto_strategy_is_unwrapped_and_planned() -> None:
    strategy, rail = structured_output_stack(_llm({}), "openai", AutoStrategy(_SCHEMA))
    assert isinstance(strategy, ToolStrategy)
    assert isinstance(rail, StructuredOutputRailMiddleware)


def test_pydantic_class_tool_tier_is_bound_under_the_class_name() -> None:
    strategy, _rail = structured_output_stack(_llm({}), "openai", _Model)
    assert isinstance(strategy, ToolStrategy)
    assert getattr(strategy.schema, "__name__", None) == "_Model"
    parsed = TypeAdapter(strategy.schema).validate_python({"value": 7})
    assert isinstance(parsed, BaseModel)
    assert parsed.model_dump()["value"] == 7


def test_recursive_pydantic_class_tool_tier_binds_the_authored_class() -> None:
    # A recursive pydantic class carries a ``$ref`` cycle; the tool tier binds the class itself,
    # which langchain's tool-calling path converts to an OpenAI tool without raising.
    strategy, _rail = structured_output_stack(_llm({}), "openai", _Node)
    assert isinstance(strategy, ToolStrategy)
    assert strategy.schema is _Node
    convert_to_openai_tool(strategy.schema)


def test_native_pydantic_class_binds_the_portable_schema() -> None:
    strategy, _rail = structured_output_stack(_llm({"structured_output": True}), "openai", _Model)
    assert isinstance(strategy, NativeStrategy)
    assert strategy.to_model_kwargs() == native_structured_output_kwargs(
        "openai", "_Model", to_portable_schema(_Model.model_json_schema())
    )


def test_a_native_json_payload_is_read_from_text_blocks_and_strings() -> None:
    from langchain_core.messages import AIMessage

    from tai42_agents._internal.structured import _native_json

    message = AIMessage(content=[{"type": "text", "text": '{"value": '}, "1", {"type": "image", "url": "x"}, "}"])

    assert _native_json(message) == {"value": 1}
    assert _native_json(AIMessage(content='{"value": 2}')) == {"value": 2}
