"""Shared tool-error recovery for every agent graph.

Two generic, platform-neutral mechanisms every agent wires in:

* :class:`ToolErrorToMessageMiddleware` — a langchain ``wrap_tool_call`` middleware
  that turns a tool-logic failure into a model-visible error ``ToolMessage`` so the
  loop continues; every other exception stays a loud abort.
* :func:`_repair_dangling_tool_calls` — a turn-start repair that answers a thread's
  unanswered tool_calls before a new run, healing a thread an aborted run poisoned.

One implementation, one owner: the tools agent, deep agent, refine loop, and
retrieval graph all reuse these — no per-agent fork.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware, ToolCallRequest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import ToolException
from langgraph.constants import START
from langgraph.types import Command
from pydantic import ValidationError

logger = logging.getLogger(__name__)

_INTERRUPTED_TOOL_RESULT = "the run was interrupted before this tool produced a result"


class ToolErrorToMessageMiddleware(AgentMiddleware):
    """Turn a tool-logic failure into a model-visible error ``ToolMessage`` so the
    agent loop continues instead of aborting with a checkpointed dangling tool_call.

    Only a ``ToolException`` or a pydantic ``ValidationError`` from the tool
    invocation is caught; every other exception propagates unchanged so an
    infrastructure failure stays a loud abort.
    """

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
    """Answer a thread's unanswered tool_calls before a new turn runs on it.

    A provider rejects a thread whose last checkpointed message is an ``AIMessage``
    carrying tool_calls that never received their ``ToolMessage`` responses (the tool
    aborted, or the process died mid-run). One synthetic error ``ToolMessage`` per
    unanswered tool_call_id is appended through the ``add_messages`` reducer so it
    lands ahead of the new user input. A fresh thread with no checkpointed messages
    is untouched, and a run paused on an interrupt — which legitimately owns its
    pending tool_calls until it resumes — is left alone.
    """
    snapshot = await agent.aget_state(config)
    messages = snapshot.values.get("messages", []) if snapshot.values else []
    if not messages:
        return
    last = messages[-1]
    tool_calls = getattr(last, "tool_calls", None) or []
    if not isinstance(last, AIMessage) or not tool_calls:
        return
    # A paused interrupt is waiting to execute these tool_calls on resume, so its
    # thread is not poisoned; only an aborted run (no pending interrupt) is repaired.
    if getattr(snapshot, "interrupts", None):
        return
    thread_id = config.get("configurable", {}).get("thread_id")
    logger.warning(
        "repairing dangling tool_calls on thread %s: %s",
        thread_id,
        [call["id"] for call in tool_calls],
    )
    await agent.aupdate_state(
        config,
        {
            "messages": [
                ToolMessage(
                    content=_INTERRUPTED_TOOL_RESULT,
                    tool_call_id=call["id"],
                    name=call["name"],
                    status="error",
                )
                for call in tool_calls
            ]
        },
        as_node=START,
    )
