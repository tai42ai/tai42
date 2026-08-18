"""``as_tool_strategy``: deterministic structured-output routing.

A raw schema (dict or pydantic class) is pinned to the tool-calling strategy;
an ``AutoStrategy`` — the auto-detect wrapper that re-enables provider-native
routing — is unwrapped and its schema pinned the same way; an explicit
``ToolStrategy``/``ProviderStrategy`` and ``None`` pass through untouched
(never double-wrapped).
"""

from __future__ import annotations

from langchain.agents.structured_output import AutoStrategy, ProviderStrategy, ToolStrategy
from pydantic import BaseModel

from tai42_agents._internal.structured import as_tool_strategy

_SCHEMA = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}


class _Model(BaseModel):
    value: int


def test_none_passes_through() -> None:
    assert as_tool_strategy(None) is None


def test_raw_dict_schema_is_pinned_to_tool_strategy() -> None:
    wrapped = as_tool_strategy(_SCHEMA)
    assert isinstance(wrapped, ToolStrategy)
    assert wrapped.schema == _SCHEMA


def test_raw_pydantic_schema_is_pinned_to_tool_strategy() -> None:
    wrapped = as_tool_strategy(_Model)
    assert isinstance(wrapped, ToolStrategy)
    assert wrapped.schema is _Model


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
    # tool-calling strategy like a raw schema.
    wrapped = as_tool_strategy(AutoStrategy(_SCHEMA))
    assert isinstance(wrapped, ToolStrategy)
    assert wrapped.schema == _SCHEMA

    wrapped_model = as_tool_strategy(AutoStrategy(_Model))
    assert isinstance(wrapped_model, ToolStrategy)
    assert wrapped_model.schema is _Model
