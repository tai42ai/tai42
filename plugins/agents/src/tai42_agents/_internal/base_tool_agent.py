"""The tools-agent factory plus its invoke and raw-stream faces.

``_build_agent_and_input`` compiles a LangGraph ``create_agent`` graph and builds
its input messages and run config. ``ainvoke_tools_agent`` runs it once and
returns the user text plus per-call usage; ``astream_tools_agent`` yields the raw
LangGraph chunks (the normalized event projection lives in ``stream_events``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import BaseMessage
from langchain_core.tools import StructuredTool
from langgraph.types import Command
from tai42_contract.agent.events import SuspendedFinal
from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry
from tai42_kit.llm.middleware.context_overflow import context_overflow_middlewares
from tai42_kit.llm.middleware.leading_user import LeadingUserMiddleware
from tai42_kit.llm.middleware.rolling_cache_mark import RollingCacheMarkMiddleware
from tai42_kit.llm.middleware.system_purge import SystemPurgeMiddleware
from tai42_kit.llm.models import get_llm_async
from tai42_kit.llm.runtime import build_agent_input, build_system_message, build_user_output, extract_structured_output
from tai42_kit.llm.settings import llm_provider_settings, llm_settings
from tai42_kit.logging.settings import logging_settings

from tai42_agents._internal.append import awrite_thread_messages
from tai42_agents._internal.config_util import init_langgraph_config
from tai42_agents._internal.park import ParkIdentity, finalize_drive, park_continuation
from tai42_agents._internal.park.middleware import AsyncParkMiddleware
from tai42_agents._internal.recovery import _repair_dangling_tool_calls, _tool_error_middleware
from tai42_agents._internal.structured import as_tool_strategy
from tai42_agents._internal.usage import AgentInvokeResult, aggregate_usage

# One shared, stateless park hook leading the tools-agent stack, so an async
# ``ask_user`` parked inside a run interrupts its own graph and resumes by id. The
# SAME hook class deep_agent mounts (the park driver is shared, not forked per agent
# type); a fresh stateless instance here keeps the tools-agent import graph a leaf.
_async_park_middleware = AsyncParkMiddleware()

#: The type of the closure a face hands the compile seam to build a park identity
#: against the FINAL (thread-id-resolved) run config — deferred so a keyless run's
#: minted thread id is captured, never a pre-init ``None``.
ParkBuilder = Callable[[dict[str, Any]], ParkIdentity | None]


def _suspended_receipt(event: SuspendedFinal) -> dict[str, Any]:
    """The suspended RECEIPT a parked run returns in place of an answer — the same
    shape :meth:`Agent._drain` yields, so a park is a clean non-error outcome."""
    return {
        "status": "suspended",
        "interaction_ids": event.interaction_ids,
        "thread_id": event.thread_id,
        "expiry_at": event.expiry_at,
    }


async def _compile_tools_agent(
    tools: list[StructuredTool],
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
    response_format: Any = None,
    system_message: str = "",
    system_content_kwargs: dict[str, Any] | None = None,
) -> Any:
    """Resolve the model and checkpointer and compile the tools agent graph.

    The build every face shares, up to compile — no model invocation happens here.
    The model resolves from the kit LLM settings (``llm_kwargs`` override the
    defaults), the checkpointer from the checkpoint registry (so every caller
    resolving the same ``checkpoint_provider`` reaches the same saver), and the
    system-purge, context-overflow, leading-user, rolling-cache-mark and
    tool-error middleware are attached.

    ``system_message`` (with optional ``system_content_kwargs``, e.g.
    ``cache_control`` for prompt caching) becomes the graph's per-run
    ``system_prompt``, applied at the model-call boundary on every turn and never
    written into checkpointed thread state; the ``SystemPurgeMiddleware`` removes
    any system message a thread's stored history carries, so state never contains
    one. A ``response_format`` (JSON-Schema dict or pydantic class) forces the
    structured output onto ``state["structured_response"]`` — always through the
    tool-calling strategy, so routing never depends on the provider; ``None``
    keeps free-form text.
    """
    llm_provider = llm_provider or llm_provider_settings().llm
    llm = await get_llm_async(provider=llm_provider, **llm_settings().with_fallbacks(llm_kwargs or {}))

    checkpoint_provider = checkpoint_provider or llm_provider_settings().checkpoint
    checkpointer = await checkpoint_registry().get_checkpointer(
        provider=checkpoint_provider,
        conn_string=llm_provider_settings().checkpoint_conn_string,
    )

    # The per-run system prompt is shared with the context-overflow middlewares so
    # the trimming budget covers the full outgoing request, prompt included.
    system_prompt = build_system_message(system_message, system_content_kwargs)
    return create_agent(
        llm,
        tools=tools,
        system_prompt=system_prompt,
        checkpointer=checkpointer,
        middleware=[
            # The park hook leads: it is the loop's first before_model hook, so it
            # recognizes an async-ask park before any message-compacting hook (which
            # compact through wrap_model_call, skipped on a park super-step) could
            # evict its marked ToolMessage.
            _async_park_middleware,
            SystemPurgeMiddleware(),
            *context_overflow_middlewares(system_prompt=system_prompt),
            LeadingUserMiddleware(),
            RollingCacheMarkMiddleware(),
            _tool_error_middleware,
        ],
        debug=logging_settings().is_enabled_for("DEBUG"),
        response_format=as_tool_strategy(response_format),
    )


async def _build_agent_and_input(
    system_message: str,
    user_message: list[str],
    tools: list[StructuredTool],
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    system_content_kwargs: dict[str, Any] | None = None,
    user_content_kwargs: dict[str, Any] | None = None,
    response_format: Any = None,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Compile the tools agent, build its input messages and run config, and ready
    the thread for the turn.

    Wraps :func:`_compile_tools_agent` with the input build and the turn-start
    repair: the single choke point every face builds through, so the thread's
    dangling tool_calls are repaired here (once the config resolves a ``thread_id``)
    for every face. The system message is compiled into the graph as its per-run
    ``system_prompt`` — the input carries only the user messages, so thread state
    stays system-free. ``system_content_kwargs`` / ``user_content_kwargs`` (e.g.
    ``cache_control``) carry content-block keys onto the system message and the last
    user message respectively. Returns ``(agent, messages, config)``.
    """
    agent = await _compile_tools_agent(
        tools,
        llm_provider=llm_provider,
        checkpoint_provider=checkpoint_provider,
        llm_kwargs=llm_kwargs,
        response_format=response_format,
        system_message=system_message,
        system_content_kwargs=system_content_kwargs,
    )

    config = init_langgraph_config(config)
    messages = build_agent_input(*user_message, user_content_kwargs=user_content_kwargs)
    await _repair_dangling_tool_calls(agent, config)
    return agent, messages, config


async def aappend_tools_agent_messages(
    messages: list[BaseMessage],
    config: dict[str, Any],
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
) -> None:
    """Append messages to the thread's checkpoint without running the model.

    Compiles the tools agent to the SAME checkpointer a run resolves (from
    ``checkpoint_provider``) and writes the pre-converted messages through the
    ``START`` node. No tool set is needed — the checkpoint write is
    tool-independent — so the graph compiles with an empty tool set.
    """
    agent = await _compile_tools_agent(
        [],
        llm_provider=llm_provider,
        checkpoint_provider=checkpoint_provider,
        llm_kwargs=llm_kwargs,
    )
    await awrite_thread_messages(agent, config, messages)


async def ainvoke_tools_agent(
    system_message: str,
    user_message: list[str],
    tools: list[StructuredTool],
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    system_content_kwargs: dict[str, Any] | None = None,
    user_content_kwargs: dict[str, Any] | None = None,
    response_format: Any = None,
    park_builder: ParkBuilder | None = None,
    resume: Any = None,
) -> AgentInvokeResult:
    """Invoke the tools agent and return the user output, per-call usage
    aggregated from every AIMessage in the run state, and the structured response.

    ``.structured`` holds the forced structured output when a ``response_format``
    was requested — validated against it, raising loudly if missing or
    non-conforming — and is ``None`` otherwise.

    ``park_builder`` (given the FINAL thread-id-resolved run config) decides whether
    the run is park-capable and, if so, captures the rebuild identity: the resume
    continuation is bound around the drive, and a run that parks on an async
    ``ask_user`` persists its durable index and returns a ``.suspended`` receipt
    instead of an answer. ``resume`` drives ``Command(resume=...)`` — answering a
    prior park — in place of a fresh user turn.
    """
    agent, messages, config = await _build_agent_and_input(
        system_message,
        user_message,
        tools,
        llm_provider,
        checkpoint_provider,
        llm_kwargs,
        config,
        system_content_kwargs=system_content_kwargs,
        user_content_kwargs=user_content_kwargs,
        response_format=response_format,
    )
    park = park_builder(config) if park_builder is not None else None
    agent_input: Any = Command(resume=resume) if resume is not None else messages
    # The resume continuation is bound for the drive's duration so an async ``ask_user``
    # a tool raises stamps its resume path onto the parked interaction (a no-op when the
    # run is not park-capable / does not bind).
    with park_continuation(park):
        state = await agent.ainvoke(agent_input, config)
        park_events = await finalize_drive(agent, config, None, park)
    for event in park_events:
        if isinstance(event, SuspendedFinal):
            return AgentInvokeResult(
                output="",
                usage=aggregate_usage(state),
                structured=None,
                suspended=_suspended_receipt(event),
            )
    structured = extract_structured_output(state, response_format) if response_format is not None else None
    return AgentInvokeResult(
        output=build_user_output(state),
        usage=aggregate_usage(state),
        structured=structured,
    )


async def astream_tools_agent(
    system_message: str,
    user_message: list[str],
    tools: list[StructuredTool],
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    stream_mode: str = "values",
    system_content_kwargs: dict[str, Any] | None = None,
    user_content_kwargs: dict[str, Any] | None = None,
    response_format: Any = None,
) -> AsyncIterator[Any]:
    """Run the tools agent and yield the raw LangGraph ``astream`` chunks for the
    requested ``stream_mode``; the caller decodes the channel shapes. A
    ``response_format`` forces structured output onto the ``structured_response``
    state channel."""
    agent, messages, config = await _build_agent_and_input(
        system_message,
        user_message,
        tools,
        llm_provider,
        checkpoint_provider,
        llm_kwargs,
        config,
        system_content_kwargs=system_content_kwargs,
        user_content_kwargs=user_content_kwargs,
        response_format=response_format,
    )
    async for chunk in agent.astream(messages, config, stream_mode=stream_mode):
        yield chunk
