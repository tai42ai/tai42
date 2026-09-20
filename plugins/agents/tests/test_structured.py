"""``as_tool_strategy``: deterministic structured-output routing.

A raw schema is pinned to the tool-calling strategy: a JSON-Schema dict and a
pydantic class are both routed through a bounded TypedDict shape (int64-tightened),
while a recursive model — which a TypedDict tree cannot express — keeps the class
itself. An ``AutoStrategy`` — the auto-detect wrapper that re-enables
provider-native routing — is unwrapped and its schema pinned the same way; an
explicit ``ToolStrategy``/``ProviderStrategy`` and ``None`` pass through untouched
(never double-wrapped).
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain.agents.structured_output import AutoStrategy, ProviderStrategy, ToolStrategy
from pydantic import BaseModel, TypeAdapter, ValidationError

from tai42_agents._internal.structured import as_tool_strategy

_SCHEMA = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}

INT64_MAX = 9223372036854775807


class _Model(BaseModel):
    value: int


class _Inner(BaseModel):
    k: int


class _Outer(BaseModel):
    inner: _Inner


class _Node(BaseModel):
    # A self-referential model: a TypedDict tree cannot express the cycle.
    next: _Node | None = None


_Node.model_rebuild()


def _validate(strategy: Any, data: dict[str, Any]) -> Any:
    """Round-trip ``data`` through the strategy's pinned schema exactly as
    langchain's tool-calling parse does (a pydantic ``TypeAdapter``)."""
    return TypeAdapter(strategy.schema).validate_python(data)


def test_none_passes_through() -> None:
    assert as_tool_strategy(None) is None


def test_raw_dict_schema_is_pinned_and_round_trips_to_a_dict() -> None:
    # A raw JSON-Schema dict is pinned to a TypedDict shape whose parse yields the
    # same plain dict — preserving the raw-schema output contract.
    wrapped = as_tool_strategy(_SCHEMA)
    assert isinstance(wrapped, ToolStrategy)
    assert _validate(wrapped, {"value": 7}) == {"value": 7}


def test_raw_dict_schema_injects_int64_bound_that_rejects_oversized_int() -> None:
    wrapped = as_tool_strategy(_SCHEMA)
    with pytest.raises(ValidationError):
        _validate(wrapped, {"value": INT64_MAX + 1})


def test_raw_pydantic_schema_is_converted_and_bound_under_class_name() -> None:
    # A BaseModel is no longer passed through as itself: it is routed through its
    # JSON schema into a bounded TypedDict shape (so its integers carry the int64
    # bound), while the TypedDict is named after the class — keeping the tool bound
    # under the class name for stream suppression.
    wrapped = as_tool_strategy(_Model)
    assert isinstance(wrapped, ToolStrategy)
    assert wrapped.schema is not _Model
    assert getattr(wrapped.schema, "__name__", None) == "_Model"
    assert _validate(wrapped, {"value": 7}) == {"value": 7}


def test_raw_pydantic_schema_bound_rejects_oversized_top_level_and_nested_ints() -> None:
    wrapped = as_tool_strategy(_Model)
    with pytest.raises(ValidationError):
        _validate(wrapped, {"value": INT64_MAX + 1})

    wrapped_nested = as_tool_strategy(_Outer)
    assert isinstance(wrapped_nested, ToolStrategy)
    assert _validate(wrapped_nested, {"inner": {"k": 1}}) == {"inner": {"k": 1}}
    with pytest.raises(ValidationError):
        _validate(wrapped_nested, {"inner": {"k": INT64_MAX + 1}})


def test_recursive_pydantic_model_falls_back_to_the_class() -> None:
    # A recursive model has no TypedDict-tree form: the converter raises and the
    # fallback keeps the class itself (its oversized-int door is closed downstream
    # by validate_structured_output's unconditional int64 walk).
    wrapped = as_tool_strategy(_Node)
    assert isinstance(wrapped, ToolStrategy)
    assert wrapped.schema is _Node


def test_explicit_tool_strategy_passes_through_unwrapped() -> None:
    # An already-explicit strategy is the caller's routing decision: it passes
    # through as the SAME object, never nested inside a second ToolStrategy.
    strategy = ToolStrategy(_SCHEMA)
    assert as_tool_strategy(strategy) is strategy


def test_explicit_provider_strategy_passes_through_unwrapped() -> None:
    strategy = ProviderStrategy(_Model)
    assert as_tool_strategy(strategy) is strategy


def test_auto_strategy_is_rerouted_to_tool_strategy() -> None:
    # AutoStrategy IS the auto-detect wrapper that re-enables provider-native
    # routing, so it does not pass through: its schema is pinned to the
    # tool-calling strategy like a raw schema (dict tightened to int64 bounds).
    wrapped = as_tool_strategy(AutoStrategy(_SCHEMA))
    assert isinstance(wrapped, ToolStrategy)
    assert _validate(wrapped, {"value": 7}) == {"value": 7}

    wrapped_model = as_tool_strategy(AutoStrategy(_Model))
    assert isinstance(wrapped_model, ToolStrategy)
    # A BaseModel unwrapped from an AutoStrategy is converted like a raw model.
    assert wrapped_model.schema is not _Model
    assert getattr(wrapped_model.schema, "__name__", None) == "_Model"
    assert _validate(wrapped_model, {"value": 7}) == {"value": 7}
