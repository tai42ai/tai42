"""Bind the kit's structured-output plan to LangChain for a compiled run.

:func:`structured_output_stack` is the ONE seam every compile path calls after the
model is resolved. It asks the kit for the capability-negotiated
:class:`~tai42_kit.llm.structured.StructuredOutputPlan` and mints the pair the graph
and the projection share:

* the strategy handed to ``create_agent(response_format=…)`` — a :class:`NativeStrategy`
  (provider-native grammar, no forced tool choice) under the native plan, or a
  ``ToolStrategy`` over the plan's ``tool_schema`` (the authored schema) under the tool plan (built with
  ``handle_errors=False`` so LangChain's own rail steps aside and the platform rail owns
  the retry), or the caller's explicit strategy (an explicit ``ToolStrategy`` is rebound with
  ``handle_errors=False`` the same way);
* the :class:`~tai42_agents._internal.structured_rail.StructuredOutputRailMiddleware`
  appended to the graph's middleware list, which validates every structured payload
  in-node against the authored schema and re-prompts under the per-run cap.

:func:`ainvoke_structured` runs the identical capped loop without a graph for the
single-shot doors, so every door ends on the same :class:`RepromptCapError` past the cap.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from typing import Any, cast

import pydantic
from langchain.agents.structured_output import (
    AutoStrategy,
    ProviderStrategy,
    StructuredOutputValidationError,
    ToolStrategy,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel
from tai42_kit.llm.models import native_structured_output_kwargs
from tai42_kit.llm.runtime import validate_structured_output
from tai42_kit.llm.structured import StructuredOutputPlan, StructuredOutputUnsupportedError, plan_structured_output
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_agents._internal.outcomes import RepromptCounter, build_reprompt_handler
from tai42_agents._internal.structured_rail import (
    StructuredOutputMissingError,
    StructuredOutputRailMiddleware,
    _broken_structured_call,
    native_text,
    reprompt_feedback,
)
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

    ``None`` (no structured output) passes through with no rail. Every other format gets
    the rail, which validates and re-prompts for both tiers:

    * an explicit ``ToolStrategy`` keeps its tier, schemas, tool names and
      ``tool_message_content``, bound with ``handle_errors=False`` so the rail is the
      single judge;
    * an explicit ``ProviderStrategy`` keeps its provider-native binding;
    * a raw JSON-Schema dict, a pydantic class, or an ``AutoStrategy`` wrapping one is
      planned by the kit and bound here.
    """
    if response_format is None:
        return None, None
    cap = agents_limits_settings().structured_output_reprompt_cap
    if isinstance(response_format, ToolStrategy):
        rebound = copy.copy(response_format)
        rebound.handle_errors = False
        names = ", ".join(spec.name for spec in response_format.schema_specs)
        return rebound, StructuredOutputRailMiddleware(response_format.schema, cap, schema_name=names)
    if isinstance(response_format, ProviderStrategy):
        rail = StructuredOutputRailMiddleware(response_format.schema, cap, schema_name=response_format.schema_spec.name)
        return response_format, rail
    schema = response_format.schema if isinstance(response_format, AutoStrategy) else response_format
    if not isinstance(schema, dict) and not (isinstance(schema, type) and issubclass(schema, BaseModel)):
        # A langchain schema shape the capability plan does not model (a Python union of
        # models, a TypedDict, a dataclass): bind it to the tool tier as-is. The rail
        # validates the produced value against it and re-prompts a non-conforming payload
        # under the per-run cap. Its outcomes name the bound tools, as an explicit
        # ``ToolStrategy`` over the same shape does.
        bound = ToolStrategy(schema, handle_errors=False)
        names = ", ".join(spec.name for spec in bound.schema_specs)
        return bound, StructuredOutputRailMiddleware(schema, cap, schema_name=names)
    plan = plan_structured_output(llm, provider, schema)
    if plan.mode == "native":
        strategy: Any = NativeStrategy(plan)
    else:
        assert plan.tool_schema is not None  # noqa: S101 (tool plan invariant; keeps the ToolStrategy call total)
        # A dict top-level ``oneOf`` fans out to one tool per variant; a dict variant parses to
        # the raw dict, so the rail is the single judge on both tiers.
        strategy = ToolStrategy(plan.tool_schema, handle_errors=False)
    rail = StructuredOutputRailMiddleware(plan.validation_schema, cap, schema_name=plan.name)
    return strategy, rail


def _native_json(message: AIMessage) -> Any:
    """Parse the JSON payload out of a native structured-output ``AIMessage`` (raises on malformed JSON)."""
    return json.loads(native_text(message))


def _native_payload(message: AIMessage) -> Any:
    """The native answer's JSON payload.

    An answer with no text beyond whitespace, or a JSON ``null``, is no structured answer and
    raises :class:`StructuredOutputMissingError`; other text that is not valid JSON raises its
    parse error.
    """
    if not native_text(message).strip():
        raise StructuredOutputMissingError()
    payload = _native_json(message)
    if payload is None:
        raise StructuredOutputMissingError()
    return payload


def _tool_payload(result: Any, raw: AIMessage | None, tool_schema: Any) -> Any:
    """The parsed value of a ``with_structured_output(include_raw=True)`` result.

    A parsing error re-raises. The parser reads only the answer's valid tool calls, so a
    call to the bound tool whose arguments did not parse (an invalid tool call on ``raw``)
    parses to nothing; it raises the graph rail's
    :class:`~langchain.agents.structured_output.StructuredOutputValidationError` carrying
    the parse error. An answer that made no structured tool call at all (empty content, or
    the answer written as message text) raises :class:`StructuredOutputMissingError`
    naming the bound tool.
    """
    if isinstance(result, dict) and result.get("parsing_error") is not None:
        raise result["parsing_error"]
    parsed = result.get("parsed") if isinstance(result, dict) else result
    if parsed is None:
        tool_name = convert_to_openai_tool(tool_schema)["function"]["name"]
        broken = _broken_structured_call(raw, [tool_name])
        if broken is not None:
            raise broken
        raise StructuredOutputMissingError([tool_name])
    return parsed


def _model_label(llm: BaseChatModel) -> str:
    """The model id/name a provider exposes, for a loud error naming the model."""
    return getattr(llm, "model_name", None) or getattr(llm, "model", None) or getattr(llm, "model_id", "") or "model"


async def ainvoke_structured(
    llm: BaseChatModel,
    plan: StructuredOutputPlan,
    messages: Sequence[BaseMessage],
    config: dict[str, Any] | None = None,
) -> Any:
    """Force structured output from a single model call, re-prompting under the per-run cap.

    Native: bind the kit kwargs, invoke, parse the JSON text, validate. Tool: bind the
    plan's ``tool_schema`` via ``with_structured_output`` (function-calling), invoke,
    validate the parsed value. A fan-out schema (a top-level ``oneOf``) is refused with
    :class:`~tai42_kit.llm.structured.StructuredOutputUnsupportedError` before any call:
    this door binds one object schema. On a malformed / non-conforming payload (a structured
    tool call whose arguments did not parse is judged with its parse error, as the graph rail
    judges it), or an answer
    that carries no structured payload at all (no structured tool call, a JSON ``null``), the
    SAME capped re-prompt loop (counter, :class:`StructuredOutputMissingError`, feedback
    shapes) as the graph rail runs; past the cap it raises
    :class:`~tai42_agents._internal.outcomes.RepromptCapError`, which each single-shot face
    maps to a typed, non-fatal outcome.

    ``config`` is the per-invoke run config carrying the run's monitoring callbacks; it is
    threaded onto every model call so this single-shot door records under the run's trace,
    exactly like the graph doors. ``None`` runs uninstrumented.
    """
    if plan.mode == "tool" and isinstance(plan.tool_schema, dict) and "oneOf" in plan.tool_schema:
        raise StructuredOutputUnsupportedError(
            model=_model_label(llm),
            provider=plan.provider,
            reasons=[
                f"response_format {plan.name!r} is a oneOf/anyOf of objects; the single-call door binds one "
                "object schema — use an agent loop face"
            ],
        )
    counter = RepromptCounter()
    reprompt = build_reprompt_handler(
        agents_limits_settings().structured_output_reprompt_cap, lambda: counter, schema_name=plan.name
    )
    conversation = list(messages)
    while True:
        exc: Exception
        if plan.mode == "native":
            assert plan.provider_schema is not None  # noqa: S101 (native plan invariant; keeps the kwargs call total)
            kwargs = native_structured_output_kwargs(plan.provider, plan.name, plan.provider_schema)
            answer = await llm.bind(**kwargs).ainvoke(conversation, cast(RunnableConfig | None, config))
            failure_message: AIMessage | None = answer if isinstance(answer, AIMessage) else None
            try:
                return validate_structured_output(_native_payload(answer), plan.validation_schema)
            except (json.JSONDecodeError, ValueError, StructuredOutputMissingError, *_VALIDATION_ERRORS) as caught:
                exc = caught
        else:
            assert plan.tool_schema is not None  # noqa: S101 (tool plan invariant; keeps the bind call total)
            bound = llm.with_structured_output(plan.tool_schema, method="function_calling", include_raw=True)
            result = await bound.ainvoke(conversation, cast(RunnableConfig | None, config))
            raw = result.get("raw") if isinstance(result, dict) else None
            failure_message = raw if isinstance(raw, AIMessage) else None
            try:
                payload = _tool_payload(result, failure_message, plan.tool_schema)
                return validate_structured_output(payload, plan.validation_schema)
            except (
                ValueError,
                StructuredOutputValidationError,
                StructuredOutputMissingError,
                *_VALIDATION_ERRORS,
            ) as caught:
                exc = caught
        text = reprompt(exc)  # raises RepromptCapError past the cap
        conversation = [*conversation, *reprompt_feedback(failure_message, text)]
