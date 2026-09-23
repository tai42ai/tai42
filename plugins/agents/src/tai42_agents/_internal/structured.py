"""Deterministic structured-output routing for graph factories."""

from __future__ import annotations

from typing import Any

from langchain.agents.structured_output import AutoStrategy, ProviderStrategy, ToolStrategy
from pydantic import BaseModel
from tai42_kit.utils.data.json_schema_util import inject_int64_bounds, json_schema_to_typed_dict

from tai42_agents._internal.outcomes import build_reprompt_handler
from tai42_agents.settings import agents_limits_settings


def _bounded_schema(schema: dict[str, Any]) -> Any:
    """Tighten a raw JSON-Schema ``response_format`` to the int64 range and pin it to an enforced ``TypedDict`` shape.

    A raw JSON-Schema dict is otherwise handed to the tool-calling strategy
    unvalidated — langchain returns the tool args as-is — so an out-of-range
    integer flows straight into ``structured_response`` and aborts the checkpoint
    serializer. Injecting int64 bounds and converting to a ``TypedDict`` makes the
    strategy's pydantic parse reject that integer, which the strategy's
    ``handle_errors`` retry rail turns into a re-prompt; a conforming value still
    round-trips to the same nested-``dict`` shape the raw schema yielded.
    """
    return json_schema_to_typed_dict(inject_int64_bounds(schema), name=schema.get("title") or "Response")


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

    A raw JSON-Schema dict, and a pydantic ``BaseModel`` class (routed through its
    JSON schema), are additionally tightened to the platform int64 range (see
    :func:`_bounded_schema`) so an oversized integer is a retryable parse failure
    rather than a serializer crash. Both faces re-validate against the ORIGINAL
    class downstream (``validate_structured_output``'s BaseModel branch re-inflates
    the parsed dict), so the ``.structured``/result contract is preserved. A
    recursive model — which a ``TypedDict`` tree cannot express — keeps the class
    itself; its oversized-int door is then closed by ``validate_structured_output``'s
    unconditional int64 walk instead.

    Every ``ToolStrategy`` built here carries a fresh per-run ``handle_errors``
    counter (:func:`~tai42_agents._internal.outcomes.build_reprompt_handler`): it
    re-prompts a non-conforming response exactly as the default rail does until the
    ``structured_output_reprompt_cap`` limit, then raises
    :class:`~tai42_agents._internal.outcomes.RepromptCapError` so the loop cannot
    run away to the recursion limit. This is the ONE seam every face builds its
    strategy through, and a strategy is built per compiled run, so the count is
    scoped to a single run. An explicit ``ToolStrategy``/``ProviderStrategy`` passed
    in already carries its own error handling and is returned unchanged (the
    identity pass-through the streaming projection re-wraps its own strategy with).
    """
    if response_format is None or isinstance(response_format, (ToolStrategy, ProviderStrategy)):
        return response_format
    handle_errors = build_reprompt_handler(agents_limits_settings().structured_output_reprompt_cap)
    schema = response_format.schema if isinstance(response_format, AutoStrategy) else response_format
    if isinstance(schema, dict):
        return ToolStrategy(_bounded_schema(schema), handle_errors=handle_errors)
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        try:
            return ToolStrategy(_bounded_schema(schema.model_json_schema()), handle_errors=handle_errors)
        except ValueError:
            # A recursive model has no TypedDict-tree form; keep the class itself
            # (it binds the tool under the class name) and let the downstream
            # unconditional int64 walk fail loudly on an oversized integer.
            return ToolStrategy(schema, handle_errors=handle_errors)
    return ToolStrategy(schema, handle_errors=handle_errors)
