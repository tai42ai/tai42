"""Shared tool-error recovery for every agent graph.

Three generic, platform-neutral mechanisms every agent wires in:

* :class:`ToolErrorToMessageMiddleware` — a langchain middleware that turns a tool-logic
  failure into a model-visible error ``ToolMessage`` so the loop continues (every other
  exception stays a loud abort), and, after each model turn, answers the turn's invalid
  tool calls (arguments that did not parse) the same way.
* :func:`_invalid_tool_call_answers` — the one helper that answers a model turn's invalid
  tool calls. A provider sends them back as tool calls on the next request, and a tool
  call with no tool result is rejected, so each one with an id and a name gets an error
  ``ToolMessage`` carrying its parse error. The middleware's model-turn hook and the
  retrieval graph's agent node both use it.
* :func:`_repair_dangling_tool_calls` — a turn-start repair that answers the tool calls of
  a thread's last model turn that have no answer yet, valid and invalid, before a new run,
  healing a thread an aborted run poisoned.

One implementation, one owner: the tools agent, deep agent, refine loop, and
retrieval graph all reuse these — no per-agent fork.
"""

from __future__ import annotations

import itertools
import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState, ToolCallRequest, hook_config
from langchain.agents.middleware.types import (
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
    PrivateStateAttr,
)
from langchain_core.messages import AIMessage, AnyMessage, InvalidToolCall, ToolMessage
from langchain_core.tools import ToolException
from langgraph.constants import START
from langgraph.runtime import Runtime
from langgraph.types import Command
from pydantic import ValidationError

logger = logging.getLogger(__name__)

_INTERRUPTED_TOOL_RESULT = "the run was interrupted before this tool produced a result"
_UNPARSED_ARGUMENTS = "the tool call arguments did not parse"


def _is_answerable(call: InvalidToolCall) -> bool:
    """Whether an invalid tool call can be answered: a tool result needs the call's id and name.

    An empty id or name counts as none. An Anthropic request drops an invalid call that has
    no name, so an answer to it would be a tool result with no tool call; an OpenAI-compatible
    request sends it with no function name, which the provider rejects whatever follows it.
    """
    return bool(call["id"]) and bool(call["name"])


def _answerable_invalid_calls(message: AIMessage) -> list[tuple[str, str, str]]:
    """The ``(id, name, parse error)`` of each invalid tool call of ``message`` that can be answered.

    Each other invalid call is logged as one that cannot be answered.
    """
    answerable: list[tuple[str, str, str]] = []
    for call in message.invalid_tool_calls:
        call_id, name, error = call["id"], call["name"], call["error"] or _UNPARSED_ARGUMENTS
        if not call_id:
            logger.warning(
                "the model returned a call to tool %r with no id whose arguments did not parse: %s", name, error
            )
        elif not name:
            logger.warning(
                "the model returned a call %s with no tool name whose arguments did not parse: %s", call_id, error
            )
        else:
            answerable.append((call_id, name, error))
    return answerable


def _invalid_tool_call_answers(message: AIMessage, answered: frozenset[str] = frozenset()) -> list[ToolMessage]:
    """An error ``ToolMessage`` for each invalid tool call of ``message`` that has an id and a name.

    An invalid tool call is one whose arguments did not parse. Each answer carries the
    call's parse error and is logged like a tool error. A call whose id is in
    ``answered`` already has its answer and is skipped. A call with no id or no name
    cannot be answered: it is logged as such.
    """
    answers: list[ToolMessage] = []
    for call_id, name, error in _answerable_invalid_calls(message):
        if call_id in answered:
            continue
        logger.warning("tool %r (call %s) failed: %s", name, call_id, error)
        answers.append(ToolMessage(content=f"Error: {error}", tool_call_id=call_id, name=name, status="error"))
    return answers


def _last_turn(messages: list[AnyMessage]) -> tuple[AIMessage | None, frozenset[str]]:
    """The last ``AIMessage`` and the ids of the tool calls already answered after it."""
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AIMessage):
            answered = frozenset(m.tool_call_id for m in messages[index + 1 :] if isinstance(m, ToolMessage))
            return message, answered
    return None, frozenset()


#: The private state field recording whether the latest model turn produced the structured
#: answer. ``structured_response`` itself keeps an earlier run's answer on the same thread,
#: so it cannot tell whether this turn produced one.
_TURN_ANSWERED_STRUCTURED = "turn_answered_structured"


class _ToolErrorState(AgentState[Any]):
    """The agent state plus the per-turn structured-answer record."""

    turn_answered_structured: NotRequired[Annotated[bool, PrivateStateAttr]]


def _record_turn(response: ModelResponse) -> ExtendedModelResponse:
    """The model turn's response, with the record of whether it produced the structured answer."""
    return ExtendedModelResponse(
        model_response=response,
        command=Command(update={_TURN_ANSWERED_STRUCTURED: response.structured_response is not None}),
    )


def _answer_invalid_tool_calls(state: AgentState[Any]) -> dict[str, Any] | None:
    """The state update answering the last model turn's invalid tool calls, if it made any.

    A turn whose calls were all invalid has no tool call to route, so the loop would end on
    it: the update sends it back to the model, which reads the errors and tries again. The
    run's recursion limit bounds that loop. A turn that produced the structured answer ends
    the loop as usual.
    """
    turn, answered = _last_turn(state["messages"])
    if turn is None:
        return None
    answers = _invalid_tool_call_answers(turn, answered)
    if not answers:
        return None
    update: dict[str, Any] = {"messages": answers}
    if not turn.tool_calls and not state.get(_TURN_ANSWERED_STRUCTURED, False):
        update["jump_to"] = "model"
    return update


class ToolErrorToMessageMiddleware(AgentMiddleware):
    """Turn a tool-logic failure into a model-visible error ``ToolMessage`` so the agent loop continues.

    Continues instead of aborting with a checkpointed dangling tool_call.
    Only a ``ToolException`` or a pydantic ``ValidationError`` from the tool
    invocation is caught; every other exception propagates unchanged so an
    infrastructure failure stays a loud abort.

    Its ``after_model`` hook answers each invalid tool call of the model turn through
    :func:`_invalid_tool_call_answers`, and sends a turn whose calls were all invalid
    back to the model unless that turn produced the structured answer, which its
    ``wrap_model_call`` records per turn. The hook runs after the model node, so it sees
    only the turn that left it: a structured-output rail (a ``wrap_model_call`` middleware
    inside this one) judges and answers a broken structured call in-node first, and that
    call never reaches this hook.
    """

    state_schema = _ToolErrorState

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ExtendedModelResponse:
        """Run the model and record whether its turn produced the structured answer."""
        return _record_turn(handler(request))

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ExtendedModelResponse:
        """Run the model and record whether its turn produced the structured answer."""
        return _record_turn(await handler(request))

    @hook_config(can_jump_to=["model"])
    def after_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Answer the model turn's invalid tool calls."""
        return _answer_invalid_tool_calls(state)

    @hook_config(can_jump_to=["model"])
    async def aafter_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:
        """Answer the model turn's invalid tool calls."""
        return _answer_invalid_tool_calls(state)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command[Any]]],
    ) -> ToolMessage | Command[Any]:
        try:
            return await handler(request)
        except (ToolException, ValidationError) as exc:
            logger.warning(
                "tool %r (call %s) failed: %s",
                request.tool_call["name"],
                request.tool_call["id"],
                exc,
                exc_info=exc,
            )
            return ToolMessage(
                content=f"Error: {exc}",
                tool_call_id=request.tool_call["id"],
                name=request.tool_call["name"],
                status="error",
            )


_tool_error_middleware = ToolErrorToMessageMiddleware()


async def _repair_dangling_tool_calls(agent: Any, config: dict[str, Any]) -> None:
    """Answer the tool calls of a thread's last model turn that have no answer, before a new turn runs on it.

    A provider rejects a thread whose last model turn carries tool calls that never
    received their ``ToolMessage`` responses (the tool aborted, or the process died
    mid-run). The turn is the thread's last ``AIMessage`` when only tool results follow
    it; some of its calls may already be answered (an invalid call is answered as the
    turn leaves the model, before the tools run). One synthetic error ``ToolMessage``
    per unanswered tool_call_id is appended through the ``add_messages`` reducer so it
    lands ahead of the new user input: each valid call is answered as interrupted, and
    each invalid call (arguments that did not parse) through
    :func:`_invalid_tool_call_answers`, with its parse error. A fresh thread with no
    checkpointed messages is untouched, and a run paused on an interrupt — which
    legitimately owns its pending tool calls until it resumes — is left alone.
    """
    snapshot = await agent.aget_state(config)
    messages = snapshot.values.get("messages", []) if snapshot.values else []
    trailing_results = list(itertools.takewhile(lambda m: isinstance(m, ToolMessage), reversed(messages)))
    turn = messages[-len(trailing_results) - 1] if len(messages) > len(trailing_results) else None
    if not isinstance(turn, AIMessage):
        return
    answered = frozenset(m.tool_call_id for m in trailing_results)
    pending = [call for call in turn.tool_calls if call["id"] not in answered]
    pending_invalid = [
        call["id"] for call in turn.invalid_tool_calls if _is_answerable(call) and call["id"] not in answered
    ]
    if not pending and not pending_invalid:
        return
    # A paused interrupt is waiting to execute these tool_calls on resume, so its
    # thread is not poisoned; only an aborted run (no pending interrupt) is repaired.
    if getattr(snapshot, "interrupts", None):
        return
    thread_id = config.get("configurable", {}).get("thread_id")
    logger.warning(
        "repairing dangling tool_calls on thread %s: %s",
        thread_id,
        [*(call["id"] for call in pending), *pending_invalid],
    )
    interrupted = [
        ToolMessage(
            content=_INTERRUPTED_TOOL_RESULT,
            tool_call_id=call["id"],
            name=call["name"],
            status="error",
        )
        for call in pending
    ]
    await agent.aupdate_state(
        config,
        {"messages": [*interrupted, *_invalid_tool_call_answers(turn, answered)]},
        as_node=START,
    )
