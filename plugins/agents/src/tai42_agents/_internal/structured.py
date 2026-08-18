"""Deterministic structured-output routing for graph factories."""

from __future__ import annotations

from typing import Any

from langchain.agents.structured_output import AutoStrategy, ProviderStrategy, ToolStrategy


def as_tool_strategy(response_format: Any) -> Any:
    """Wrap a schema ``response_format`` in an explicit ``ToolStrategy``.

    A raw schema (JSON-Schema dict or pydantic class) handed to a graph factory
    is auto-routed: models flagged with native structured output get the
    provider-native path, which rejects moderately complex schemas. Wrapping in
    ``ToolStrategy`` pins the tool-calling path — uniform across providers, with
    far higher schema limits — while populating the same ``structured_response``
    state channel. An ``AutoStrategy`` is that same auto-routing spelled as an
    object, so its schema is pinned identically. ``None`` (no structured output)
    and an explicit ``ToolStrategy``/``ProviderStrategy`` pass through unchanged.
    """
    if response_format is None or isinstance(response_format, (ToolStrategy, ProviderStrategy)):
        return response_format
    if isinstance(response_format, AutoStrategy):
        return ToolStrategy(response_format.schema)
    return ToolStrategy(response_format)
