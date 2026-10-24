"""The per-run structured-output re-prompt rail, validating in-node against the authored schema.

One ``wrap_model_call`` middleware is the single place a structured payload is judged
before it can reach graph state, for BOTH the native and the tool plan. Inside one
model node it:

* treats a factory ``StructuredOutputError`` (native malformed JSON, or a tool-mode
  parse / multiple-tool-call failure the factory raises because the ``ToolStrategy`` is
  built with ``handle_errors=False``) as a re-prompt,
* runs :func:`~tai42_kit.llm.runtime.validate_structured_output` on every produced
  ``structured_response`` against the ORIGINAL authored schema — so a well-formed but
  non-conforming native payload (an oversized int64, an enum or constraint violation the
  provider folded into description) is caught in-node, the same place and the same
  counter as tool mode's TypedDict parse failure,
* treats a structured-output tool call whose arguments did not parse (an invalid tool
  call naming a structured-output tool) as a parse failure of that tool, even when the
  same parallel turn also calls other tools, and
* treats a final turn that carries no structured answer as a re-prompt: a turn with no
  ``structured_response`` and no tool call ends the agent loop, so empty content, the
  answer written as message text, or a native JSON ``null`` is judged as a
  :class:`StructuredOutputMissingError`. Under the native plan the factory raises on an
  empty answer (a failed JSON decode) and on a ``null`` a pydantic schema rejects; the rail
  judges both answers as missing too, while text that is not valid JSON stays a parse
  failure.

A turn that calls other tools is an interim turn and passes unjudged, and so is a turn
whose calls to other tools all failed to parse: the shared tool-error middleware answers
those calls with their parse errors and calls the model again, bounded by the run's
recursion limit. Only a missing or broken structured answer is judged. Under the native
plan, a turn whose text carries a decoded JSON answer (a value other than ``null``) is
judged against the schema on every schema kind, even when its only calls are broken calls
to other tools.

Each failure is fed back with the same ``STRUCTURED_OUTPUT_ERROR_TEMPLATE`` text the
default rail uses (as a ``ToolMessage`` answering each tool call of the offending turn, or
a ``HumanMessage`` when it made none) and the model is re-prompted up to the per-run
cap; past the cap the shared counter raises
:class:`~tai42_agents._internal.outcomes.RepromptCapError`, which every face maps to a
typed, non-fatal outcome. The counter lives on the middleware instance, which is built
fresh per compiled run, so the count is run-scoped. Failed attempts stay inside the
model node and are never written to checkpointed thread history.

The rail runs inside the model node, innermost; the shared tool-error middleware's
``after_model`` hook runs after the node, on the turn that left it. So a broken structured
call is answered once, by the rail's feedback, and only a turn the rail let through (an
interim turn, or a conforming answer) has its other invalid tool calls answered by the
hook.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pydantic
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.agents.structured_output import (
    ProviderStrategy,
    StructuredOutputError,
    StructuredOutputValidationError,
    ToolStrategy,
)
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from tai42_kit.llm.runtime import validate_structured_output
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_agents._internal.outcomes import build_reprompt_handler, drive_reprompt_counter
from tai42_agents._internal.recovery import _UNPARSED_ARGUMENTS, _answerable_invalid_calls, _is_answerable

#: A produced value that is well-formed but does not conform to the authored schema.
_VALIDATION_ERRORS = (JsonSchemaValidationError, pydantic.ValidationError)


class StructuredOutputMissingError(Exception):
    """A final model turn that carries no structured answer.

    ``tool_names`` are the structured-output tools the answer must be returned through
    (the tool plan); empty when the answer is the turn's JSON text (the native plan). The
    message is what the re-prompt feeds back to the model and what the cap outcome reports.
    """

    def __init__(self, tool_names: Sequence[str] = ()) -> None:
        """Name the structured-output tools the answer must be returned through, if any."""
        self.tool_names = list(tool_names)
        if self.tool_names:
            quoted = ", ".join(repr(name) for name in self.tool_names)
            target = f"the {quoted} tool" if len(self.tool_names) == 1 else f"one of the {quoted} tools"
            message = (
                f"no structured answer was returned: return the final answer by calling {target} "
                "with arguments that match its schema, not as message text"
            )
        else:
            message = (
                "no structured answer was returned: return the final answer as JSON that matches the response schema"
            )
        super().__init__(message)


def native_text(message: AIMessage) -> str:
    """The text of a native structured-output answer, read the way the provider strategy reads it.

    String content is the text; list content joins its text blocks, the string ``content`` of
    any other block that carries one, and every non-block part.
    """
    content = message.content
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text" and "text" in part:
                parts.append(str(part["text"]))
            elif isinstance(part.get("content"), str):
                parts.append(part["content"])
        else:
            parts.append(str(part))
    return "".join(parts)


def _decode_native(answer: AIMessage) -> tuple[bool, Any]:
    """``(True, value)`` when a native answer's text decodes as JSON, else ``(False, None)``."""
    try:
        return True, json.loads(native_text(answer))
    except json.JSONDecodeError:
        return False, None


def _decoded_native_answer(answer: AIMessage | None) -> bool:
    """Whether a native answer's text decodes as JSON to a value other than ``null``."""
    if answer is None:
        return False
    decoded, value = _decode_native(answer)
    return decoded and value is not None


def _carries_no_native_answer(answer: AIMessage | None) -> bool:
    """Whether a native answer sent no structured answer: no tool call, and blank text or a JSON ``null``.

    Text that does not decode as JSON is an answer that failed to parse, not a missing one.
    """
    if answer is None or answer.tool_calls:
        return False
    if not native_text(answer).strip():
        return True
    decoded, value = _decode_native(answer)
    return decoded and value is None


def _factory_failure(caught: StructuredOutputError, response_format: Any) -> Exception:
    """The failure a factory structured-output error is judged as.

    The provider strategy parses the answer's text as JSON and validates it against the
    schema, so an empty answer fails the decode and a JSON ``null`` fails a pydantic
    schema's validation. Neither carries a structured answer, so under a provider strategy
    both are judged as a :class:`StructuredOutputMissingError`, as a ``null`` that a dict
    schema lets through is. Every other factory error stands as raised.
    """
    if isinstance(response_format, ProviderStrategy) and _carries_no_native_answer(caught.ai_message):
        return StructuredOutputMissingError()
    return caught


def _structured_tool_names(response_format: Any) -> list[str]:
    """The structured-output tool names a ``ToolStrategy`` binds; none for any other format."""
    if isinstance(response_format, ToolStrategy):
        return [spec.name for spec in response_format.schema_specs]
    return []


def _broken_structured_call(
    answer: AIMessage | None, structured_tools: Sequence[str]
) -> StructuredOutputValidationError | None:
    """The parse failure of the first structured-output tool call whose arguments did not parse, if any."""
    if answer is None:
        return None
    for call in answer.invalid_tool_calls:
        if call["name"] is not None and call["name"] in structured_tools:
            reason = call["error"] or _UNPARSED_ARGUMENTS
            return StructuredOutputValidationError(call["name"], ValueError(reason), answer)
    return None


def _only_broken_calls(answer: AIMessage | None) -> bool:
    """Whether a turn's only tool calls are calls whose arguments did not parse, at least one answerable."""
    return (
        answer is not None and not answer.tool_calls and any(_is_answerable(call) for call in answer.invalid_tool_calls)
    )


def _is_interim(answer: AIMessage | None) -> bool:
    """Whether a turn with no structured answer continues the loop: it calls other tools.

    A call to another tool whose arguments did not parse counts: it is answered with its
    parse error after the turn, and the model is called again to read it.
    """
    return answer is not None and (bool(answer.tool_calls) or _only_broken_calls(answer))


def _last_ai_message(messages: list[BaseMessage]) -> AIMessage | None:
    """The last ``AIMessage`` in a model response's result (the model's offending answer)."""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    return None


def reprompt_feedback(failure_message: AIMessage | None, text: str) -> list[AnyMessage]:
    """The messages to append for one re-prompt: the offending answer plus the feedback turn.

    When the offending answer carried tool calls, the feedback answers each with the
    re-prompt text (a ``ToolMessage`` per call, as the default rail does). That includes
    each invalid tool call (arguments that did not parse) that has an id and a name: a
    provider sends those back as tool calls, and every tool call needs its answer. An
    invalid call with no id or no name cannot be answered and is logged.
    Otherwise (a text answer) it is a single ``HumanMessage``. A missing answer message
    degrades to the bare ``HumanMessage`` so the loop still re-prompts.
    """
    if failure_message is None:
        return [HumanMessage(content=text)]
    answers: list[AnyMessage] = [
        ToolMessage(content=text, tool_call_id=call["id"], name=call["name"]) for call in failure_message.tool_calls
    ]
    answers.extend(
        ToolMessage(content=text, tool_call_id=call_id, name=name)
        for call_id, name, _ in _answerable_invalid_calls(failure_message)
    )
    if answers:
        return [failure_message, *answers]
    return [failure_message, HumanMessage(content=text)]


class StructuredOutputRailMiddleware(AgentMiddleware):
    """Validate every structured payload in-node and re-prompt under the per-run cap.

    ``validation_schema`` is the ORIGINAL authored dict or pydantic class (what every
    door validates against); ``cap`` is the per-run re-prompt ceiling, counted per drive
    (the driving code binds :func:`~tai42_agents._internal.outcomes.drive_reprompt_scope`). The rail is the
    innermost ``wrap_model_call`` middleware so its handler is the model execution that
    parses the structured output.
    """

    def __init__(self, validation_schema: object, cap: int, *, schema_name: str | None = None) -> None:
        """Carry the authored validation schema (named ``schema_name``) and the re-prompt handler counting per drive."""
        super().__init__()
        self.tools = []
        self._validation_schema = validation_schema
        self._reprompt = build_reprompt_handler(cap, lambda: drive_reprompt_counter(self), schema_name=schema_name)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Run the model, judge its structured answer, and re-prompt in-node until it conforms or the cap trips."""
        # The drive's own counter, resolved up front so a drive with no scope fails loudly at once.
        drive_reprompt_counter(self)
        current = request
        while True:
            exc: Exception
            try:
                response = await handler(current)
            except StructuredOutputError as caught:
                if _only_broken_calls(caught.ai_message) and not _decoded_native_answer(caught.ai_message):
                    # The native plan parses a turn with no tool call as the answer; a turn whose
                    # only calls are broken calls to other tools, and whose text carries no
                    # decoded answer, is an interim turn instead.
                    return ModelResponse(result=[caught.ai_message], structured_response=None)
                failure_message: AIMessage | None = caught.ai_message
                exc = _factory_failure(caught, current.response_format)
            else:
                if response.structured_response is None:
                    answer = _last_ai_message(response.result)
                    broken = _broken_structured_call(answer, _structured_tool_names(current.response_format))
                    if broken is not None:
                        # The turn tried to answer, but the structured call's arguments did not
                        # parse: judge it even when the same turn also calls other tools.
                        failure_message = answer
                        exc = broken
                    elif _is_interim(answer):
                        # An interim turn calling other tools: the loop continues, nothing to judge.
                        return response
                    else:
                        # A turn with no tool call ends the loop, so it is the final answer, and it has none.
                        failure_message = answer
                        exc = StructuredOutputMissingError(_structured_tool_names(current.response_format))
                else:
                    try:
                        normalized = validate_structured_output(response.structured_response, self._validation_schema)
                    except _VALIDATION_ERRORS as caught:
                        failure_message = _last_ai_message(response.result)
                        exc = caught
                    else:
                        # Store exactly what the shared validator normalizes to, so graph state
                        # matches the single-shot door: a dict-authored schema yields a plain dict
                        # (the tool tier's pydantic instance dumped to its JSON-native form, keeping
                        # only the fields the model set — an omitted optional stays absent rather
                        # than a schema-breaking ``null``), while a pydantic-class schema keeps its
                        # validated instance.
                        response.structured_response = normalized
                        return response
            # Raises RepromptCapError past the cap; otherwise returns the feedback text.
            text = self._reprompt(exc)
            current = current.override(messages=[*current.messages, *reprompt_feedback(failure_message, text)])
