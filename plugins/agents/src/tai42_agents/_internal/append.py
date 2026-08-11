"""Append messages to an agent thread's checkpointed history without running the model.

The generic half of every graph-backed agent's ``append_thread_messages``: validate
and convert the role/content dicts to langchain messages, require the run config to
name a thread, and write the messages through the graph's ``START`` node so they land
ahead of the next turn — the same ``aupdate_state`` write
:mod:`tai42_agents._internal.recovery` uses, never a model invocation. Each agent
supplies the compiled graph (built its own run's way, to the same checkpointer) and
the run config; a config carrying no ``thread_id`` is an error here rather than a
minted thread the appended messages would be lost on.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langgraph.constants import START

from tai42_agents._internal.recovery import _repair_dangling_tool_calls

_ROLE_MESSAGE = {"user": HumanMessage, "assistant": AIMessage}


def to_thread_messages(messages: list[dict[str, str]]) -> list[BaseMessage]:
    """Convert ``{"role", "content"}`` dicts to langchain messages, raising on a bad one.

    ``role`` must be ``"user"`` (→ ``HumanMessage``) or ``"assistant"`` (→
    ``AIMessage``); ``content`` must be a non-blank string. A non-mapping item, an
    unknown role, or a blank/non-string content is a ``ValueError`` naming the
    offending item's index rather than a silently dropped or coerced message.
    """
    converted: list[BaseMessage] = []
    for index, item in enumerate(messages):
        if not isinstance(item, dict):
            raise ValueError(
                f"append message {index} must be a mapping with 'role' and 'content'; got {type(item).__name__}"
            )
        role = item.get("role")
        factory = _ROLE_MESSAGE.get(role) if isinstance(role, str) else None
        if factory is None:
            raise ValueError(f"append message {index} has role {role!r}; only 'user' or 'assistant' are accepted")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ValueError(f"append message {index} has blank content; a non-empty string is required")
        converted.append(factory(content=content))
    return converted


def require_thread_id(qualified_face: str, config: dict[str, Any]) -> None:
    """Raise when the resolved run config pins no ``thread_id``.

    An append targets an existing thread's checkpointed history, so a missing
    ``thread_id`` cannot be papered over with a minted one — it names no thread to
    append to and is rejected loudly, ``qualified_face`` opening the message.
    """
    if not config.get("configurable", {}).get("thread_id"):
        raise ValueError(f"{qualified_face} requires a thread_id naming the thread to append to")


async def awrite_thread_messages(agent: Any, config: dict[str, Any], messages: list[BaseMessage]) -> None:
    """Write the converted messages to the thread's checkpoint through the ``START`` node.

    The one owner of the checkpoint-only append write, and the shared seam that gives
    every append entry point the run path's heal-first semantics: the thread's
    dangling tool_calls are repaired before the write, or an appended message would
    push an unanswered tool_call into the history's interior where the turn-start
    guard can no longer reach it, poisoning every future turn. ``as_node=START`` then
    lands the messages through the ``add_messages`` reducer ahead of the next turn
    without running the graph, so no model call is made.
    """
    await _repair_dangling_tool_calls(agent, config)
    await agent.aupdate_state(config, {"messages": messages}, as_node=START)
