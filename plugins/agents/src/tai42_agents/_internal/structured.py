"""Bind the kit's structured-output plan to LangChain for a compiled run.

:func:`structured_output_stack` is the ONE seam every compile path calls after the
model is resolved. It asks the kit for the capability-negotiated
:class:`~tai42_kit.llm.structured.StructuredOutputPlan` and mints the pair the graph
and the projection share:

* the strategy handed to ``create_agent(response_format=…)`` — a :class:`NativeStrategy`
  (provider-native grammar, no forced tool choice) under the native plan, or a
  ``ToolStrategy`` over the int64-bounded pydantic model under the tool plan (built with
  ``handle_errors=False`` so LangChain's own rail steps aside and the platform rail owns
  the retry);
* the :class:`~tai42_agents._internal.structured_rail.StructuredOutputRailMiddleware`
  appended to the graph's middleware list, which validates every structured payload
  in-node against the authored schema and re-prompts under the per-run cap.

:func:`ainvoke_structured` runs the identical capped loop without a graph for the
single-shot doors, so every door ends on the same :class:`RepromptCapError` past the cap.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, cast

import pydantic
from langchain.agents.structured_output import AutoStrategy, ProviderStrategy, ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel
from tai42_kit.llm.models import native_structured_output_kwargs
from tai42_kit.llm.runtime import validate_structured_output
from tai42_kit.llm.structured import StructuredOutputPlan, plan_structured_output
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_agents._internal.outcomes import build_reprompt_handler
from tai42_agents._internal.structured_rail import StructuredOutputRailMiddleware, reprompt_feedback
from tai42_agents.settings import agents_limits_settings

#: A produced value that is well-formed but does not conform to the authored schema.
_VALIDATION_ERRORS = (JsonSchemaValidationError, pydantic.ValidationError)


class NativeStrategy(ProviderStrategy):
    """A ``ProviderStrategy`` whose model kwargs are the kit's provider-native binding.

    The factory's ``isinstance(..., ProviderStrategy)`` branch binds :meth:`to_model_kwargs`
    with the tools (no forced ``tool_choice``) and parses the final text turn with the
    ``ProviderStrategyBinding``; a dict schema parses to a plain dict, which the rail then
    validates faithfully against the ORIGINAL authored schema.
    """

    def __init__(self, plan: StructuredOutputPlan) -> None:
        """Build from a native plan: the portable schema drives parsing, the kit kwargs the bind."""
        if plan.provider_schema is None:
            raise ValueError("NativeStrategy requires a native plan with a provider_schema")
        super().__init__(plan.provider_schema)
        self._kwargs = native_structured_output_kwargs(plan.provider, plan.name, plan.provider_schema)

    def to_model_kwargs(self) -> dict[str, Any]:
        """The kit's provider-native bind kwargs (what the provider receives)."""
        return self._kwargs


def structured_output_stack(
    llm: BaseChatModel, provider: str, response_format: Any
) -> tuple[Any, StructuredOutputRailMiddleware | None]:
    """Mint the ``(strategy, rail)`` for a compiled run from the authored ``response_format``.

    ``None`` (no structured output) and an explicit ``ToolStrategy``/``ProviderStrategy``
    pass through with no rail — the caller's own routing and error handling stand. A raw
    JSON-Schema dict, a pydantic class, or an ``AutoStrategy`` wrapping one is planned by
    the kit and bound here; the rail validates and re-prompts for both tiers.
    """
    if response_format is None or isinstance(response_format, (ToolStrategy, ProviderStrategy)):
        return response_format, None
    schema = response_format.schema if isinstance(response_format, AutoStrategy) else response_format
    cap = agents_limits_settings().structured_output_reprompt_cap
    if not isinstance(schema, dict) and not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        # A langchain schema shape the capability plan does not model (a Python union of
        # models, a TypedDict, a dataclass): bind it to the tool tier as-is. The rail
        # validates the produced value against it and re-prompts a non-conforming payload
        # under the per-run cap.
        return ToolStrategy(schema, handle_errors=False), StructuredOutputRailMiddleware(schema, cap)
    plan = plan_structured_output(llm, provider, schema)
    if plan.mode == "native":
        strategy: Any = NativeStrategy(plan)
    else:
        assert plan.bound_typed_dict is not None  # noqa: S101 (tool plan invariant; keeps the ToolStrategy call total)
        strategy = ToolStrategy(plan.bound_typed_dict, handle_errors=False)
    rail = StructuredOutputRailMiddleware(plan.validation_schema, cap)
    return strategy, rail


def _native_json(message: AIMessage) -> Any:
    """Parse the JSON payload out of a native structured-output ``AIMessage`` (raises on malformed JSON)."""
    content = message.content
    if isinstance(content, str):
        text = content
    else:
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and "text" in block:
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        text = "".join(parts)
    return json.loads(text)


async def ainvoke_structured(
    llm: BaseChatModel,
    plan: StructuredOutputPlan,
    messages: Sequence[BaseMessage],
    config: dict[str, Any] | None = None,
) -> Any:
    """Force structured output from a single model call, re-prompting under the per-run cap.

    Native: bind the kit kwargs, invoke, parse the JSON text, validate. Tool: bind the
    int64-bounded pydantic model via ``with_structured_output`` (function-calling), invoke,
    validate the parsed value. On a malformed / non-conforming payload the SAME capped
    re-prompt loop (counter + feedback shapes) as the graph rail runs; past the cap it
    raises :class:`~tai42_agents._internal.outcomes.RepromptCapError`, which each
    single-shot face maps to a typed, non-fatal outcome.

    ``config`` is the per-invoke run config carrying the run's monitoring callbacks; it is
    threaded onto every model call so this single-shot door records under the run's trace,
    exactly like the graph doors. ``None`` runs uninstrumented.
    """
    reprompt = build_reprompt_handler(agents_limits_settings().structured_output_reprompt_cap)
    conversation = list(messages)
    while True:
        exc: Exception
        if plan.mode == "native":
            assert plan.provider_schema is not None  # noqa: S101 (native plan invariant; keeps the kwargs call total)
            kwargs = native_structured_output_kwargs(plan.provider, plan.name, plan.provider_schema)
            answer = await llm.bind(**kwargs).ainvoke(conversation, cast(RunnableConfig | None, config))
            failure_message: AIMessage | None = answer if isinstance(answer, AIMessage) else None
            try:
                return validate_structured_output(_native_json(answer), plan.validation_schema)
            except (json.JSONDecodeError, ValueError, *_VALIDATION_ERRORS) as caught:
                exc = caught
        else:
            assert plan.bound_typed_dict is not None  # noqa: S101 (tool plan invariant; keeps the bind call total)
            bound = llm.with_structured_output(plan.bound_typed_dict, method="function_calling", include_raw=True)
            result = await bound.ainvoke(conversation, cast(RunnableConfig | None, config))
            raw = result.get("raw") if isinstance(result, dict) else None
            failure_message = raw if isinstance(raw, AIMessage) else None
            try:
                if isinstance(result, dict) and result.get("parsing_error") is not None:
                    raise result["parsing_error"]
                parsed = result.get("parsed") if isinstance(result, dict) else result
                return validate_structured_output(parsed, plan.validation_schema)
            except (ValueError, *_VALIDATION_ERRORS) as caught:
                exc = caught
        text = reprompt(exc)  # raises RepromptCapError past the cap
        conversation = [*conversation, *reprompt_feedback(failure_message, text)]
