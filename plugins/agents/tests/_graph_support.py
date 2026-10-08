"""Drive the tools-agent faces from flat run inputs: build the graph through the cache, then drive it.

Each helper takes a run's inputs in one call (the system message, the user messages, live tools,
the model and checkpoint providers, the structured-output format), builds the run's graph with
:func:`tools_agent_graph` and drives the face under test over it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.tools import StructuredTool
from tai42_contract.agent.events import StreamEvent

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal.graph_cache import ToolsAgentGraph, ToolsAgentGraphSpec, tools_agent_graph
from tai42_agents._internal.stream_events import astream_tools_agent_events


async def compiled_graph(
    system_message: str = "",
    tools: Sequence[StructuredTool] = (),
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
    system_content_kwargs: dict[str, Any] | None = None,
    response_format: Any = None,
) -> ToolsAgentGraph:
    """The run's graph for these inputs, through the graph cache."""
    return await tools_agent_graph(
        ToolsAgentGraphSpec(
            live_tools=tuple(tools),
            system_message=system_message,
            system_content_kwargs=system_content_kwargs,
            response_format=response_format,
            llm_provider=llm_provider,
            llm_kwargs=llm_kwargs,
            checkpoint_provider=checkpoint_provider,
        )
    )


async def build_agent_and_input(
    system_message: str,
    user_message: list[str],
    tools: Sequence[StructuredTool],
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    system_content_kwargs: dict[str, Any] | None = None,
    user_content_kwargs: dict[str, Any] | None = None,
    response_format: Any = None,
) -> tuple[Any, Any, dict[str, Any], Any, str | None]:
    """``(agent, messages, config, strategy, minted_thread)`` of a run built from these inputs."""
    graph = await compiled_graph(
        system_message, tools, llm_provider, checkpoint_provider, llm_kwargs, system_content_kwargs, response_format
    )
    messages, config, minted_thread = await bta._build_agent_and_input(graph, user_message, config, user_content_kwargs)
    return graph.agent, messages, config, graph.strategy, minted_thread


async def invoke_tools_agent(
    system_message: str,
    user_message: list[str],
    tools: Sequence[StructuredTool],
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    system_content_kwargs: dict[str, Any] | None = None,
    user_content_kwargs: dict[str, Any] | None = None,
    response_format: Any = None,
    park_builder: Any = None,
    resume: Any = None,
) -> Any:
    """``ainvoke_tools_agent`` over the graph built from these inputs."""
    graph = await compiled_graph(
        system_message, tools, llm_provider, checkpoint_provider, llm_kwargs, system_content_kwargs, response_format
    )
    return await bta.ainvoke_tools_agent(
        graph, user_message, config, user_content_kwargs, park_builder=park_builder, resume=resume
    )


async def stream_tools_agent(
    system_message: str,
    user_message: list[str],
    tools: Sequence[StructuredTool],
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    stream_mode: str = "values",
    system_content_kwargs: dict[str, Any] | None = None,
    user_content_kwargs: dict[str, Any] | None = None,
    response_format: Any = None,
) -> AsyncIterator[Any]:
    """``astream_tools_agent`` over the graph built from these inputs."""
    graph = await compiled_graph(
        system_message, tools, llm_provider, checkpoint_provider, llm_kwargs, system_content_kwargs, response_format
    )
    async for chunk in bta.astream_tools_agent(graph, user_message, config, stream_mode, user_content_kwargs):
        yield chunk


async def stream_tools_agent_events(
    system_message: str,
    user_message: list[str],
    tools: Sequence[StructuredTool],
    llm_provider: str | None = None,
    checkpoint_provider: str | None = None,
    llm_kwargs: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    system_content_kwargs: dict[str, Any] | None = None,
    user_content_kwargs: dict[str, Any] | None = None,
    response_format: Any = None,
    park_builder: Any = None,
    resume: Any = None,
) -> AsyncIterator[StreamEvent]:
    """``astream_tools_agent_events`` over the graph built from these inputs."""
    graph = await compiled_graph(
        system_message, tools, llm_provider, checkpoint_provider, llm_kwargs, system_content_kwargs, response_format
    )
    async for event in astream_tools_agent_events(
        graph, user_message, config, user_content_kwargs, park_builder=park_builder, resume=resume
    ):
        yield event
