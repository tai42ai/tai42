"""The per-run structured-output re-prompt rail, validating in-node against the authored schema.

One ``wrap_model_call`` middleware is the single place a structured payload is judged
before it can reach graph state, for BOTH the native and the tool plan. Inside one
model node it:

* treats a factory ``StructuredOutputError`` (native malformed JSON, or a tool-mode
  parse / multiple-tool-call failure the factory raises because the ``ToolStrategy`` is
  built with ``handle_errors=False``) as a re-prompt, and
* runs :func:`~tai42_kit.llm.runtime.validate_structured_output` on every produced
  ``structured_response`` against the ORIGINAL authored schema — so a well-formed but
  non-conforming native payload (an oversized int64, an enum or constraint violation the
  provider folded into description) is caught in-node, the same place and the same
  counter as tool mode's TypedDict parse failure.

Each failure is fed back with the same ``STRUCTURED_OUTPUT_ERROR_TEMPLATE`` text the
default rail uses (as a ``ToolMessage`` answering the structured tool call, or a
``HumanMessage`` under the native plan) and the model is re-prompted up to the per-run
cap; past the cap the shared counter raises
:class:`~tai42_agents._internal.outcomes.RepromptCapError`, which every face maps to a
typed, non-fatal outcome. The counter lives on the middleware instance, which is built
fresh per compiled run, so the count is run-scoped. Failed attempts stay inside the
model node and are never written to checkpointed thread history.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pydantic
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain.agents.structured_output import StructuredOutputError
from langchain_core.messages import AIMessage, AnyMessage, BaseMessage, HumanMessage, ToolMessage
from tai42_kit.llm.runtime import validate_structured_output
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError

from tai42_agents._internal.outcomes import build_reprompt_handler, drive_reprompt_counter

#: A produced value that is well-formed but does not conform to the authored schema.
_VALIDATION_ERRORS = (JsonSchemaValidationError, pydantic.ValidationError)


def _last_ai_message(messages: list[BaseMessage]) -> AIMessage | None:
    """The last ``AIMessage`` in a model response's result (the model's offending answer)."""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            return message
    return None


def reprompt_feedback(failure_message: AIMessage | None, text: str) -> list[AnyMessage]:
    """The messages to append for one re-prompt: the offending answer plus the feedback turn.

    When the offending answer carried structured tool calls, the feedback answers each
    with the re-prompt text (a ``ToolMessage`` per call, as the default rail does);
    otherwise (a native text answer) it is a single ``HumanMessage``. A missing answer
    message degrades to the bare ``HumanMessage`` so the loop still re-prompts.
    """
    if failure_message is None:
        return [HumanMessage(content=text)]
    tool_calls = getattr(failure_message, "tool_calls", None)
    if tool_calls:
        answers: list[AnyMessage] = [
            ToolMessage(content=text, tool_call_id=call["id"], name=call["name"]) for call in tool_calls
        ]
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

    def __init__(self, validation_schema: object, cap: int) -> None:
        """Carry the authored validation schema and the re-prompt handler counting per drive."""
        super().__init__()
        self.tools = []
        self._validation_schema = validation_schema
        self._reprompt = build_reprompt_handler(cap, lambda: drive_reprompt_counter(self))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Run the model, judge its structured payload, and re-prompt in-node until it conforms or the cap trips."""
        # The drive's own counter, resolved up front so a drive with no scope fails loudly at once.
        drive_reprompt_counter(self)
        current = request
        while True:
            exc: Exception
            try:
                response = await handler(current)
            except StructuredOutputError as caught:
                failure_message: AIMessage | None = caught.ai_message
                exc = caught
            else:
                if response.structured_response is None:
                    # An interim tool-calling turn, or no structured format: nothing to judge.
                    return response
                try:
                    normalized = validate_structured_output(response.structured_response, self._validation_schema)
                except _VALIDATION_ERRORS as caught:
                    failure_message = _last_ai_message(response.result)
                    exc = caught
                else:
                    # Store exactly what the shared validator normalizes to, so graph state
                    # matches the single-shot door: a dict-authored schema yields a plain dict
                    # (the tool tier's pydantic instance dumped to its JSON-native form, keeping
                    # only the fields the model set — an omitted optional stays absent rather than
                    # a schema-breaking ``null``), while a pydantic-class schema keeps its
                    # validated instance.
                    response.structured_response = normalized
                    return response
            # Raises RepromptCapError past the cap; otherwise returns the feedback text.
            text = self._reprompt(exc)
            current = current.override(messages=[*current.messages, *reprompt_feedback(failure_message, text)])
