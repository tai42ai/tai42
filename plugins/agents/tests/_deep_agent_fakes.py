"""Scripted compiled-graph fakes shared by the ``langchain_deep_agent`` test modules.

The deep-agent run wiring, astream projection and subagent-spec tests all drive the
agent over a scripted stand-in graph (no live LLM, no real store); these helpers are
the shared drive rig they build on.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, Protocol

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langchain_core.tools import StructuredTool

from tai42_agents.langchain_deep_agent.agent import DeepAgent


def _client_tool(name: str) -> StructuredTool:
    async def call_tool(**kwargs: object) -> object:
        return kwargs

    return StructuredTool.from_function(
        func=None,
        coroutine=call_tool,
        name=name,
        description="client tool",
        args_schema={"type": "object", "properties": {}, "required": []},
    )


class _CompiledGraphLike(Protocol):
    """The compiled-graph surface the deep agent drives: ``astream`` replays the
    scripted ``(mode, chunk)`` pairs and ``aget_state`` reports a run's interrupts.
    Both ``_FakeCompiledGraph`` and ``_ClaimingGraph`` are scripted stand-ins that
    satisfy it, so the install helper accepts either."""

    def astream(self, agent_input: Any, config: Any, stream_mode: Any = ...) -> AsyncIterator[Any]: ...

    async def aget_state(self, config: Any, subgraphs: bool = ...) -> Any: ...


class _FakeCompiledGraph:
    """A scripted compiled graph: ``astream`` replays fixed ``(mode, chunk)``
    pairs and ``aget_state`` reports the interrupts a paused run waits on."""

    def __init__(self, chunks: list[tuple[str, Any]], interrupts: list[Any]) -> None:
        self._chunks = chunks
        self._interrupts = interrupts
        self.received_input: Any = None
        self.received_config: Any = None

    async def astream(self, agent_input: Any, config: Any, stream_mode: Any = None) -> AsyncIterator[tuple[str, Any]]:
        self.received_input = agent_input
        self.received_config = config
        for chunk in self._chunks:
            yield chunk

    async def aget_state(self, config: Any, subgraphs: bool = False) -> SimpleNamespace:
        # A faithful StateSnapshot exposes ``values`` (read by the turn-start repair),
        # ``interrupts`` (read by the interrupt projection), and ``tasks`` (each task's
        # ``interrupts`` are what the drive finalizer walks, descending into subgraphs). An
        # empty message log means a non-poisoned thread, so the repair is a no-op.
        task = SimpleNamespace(interrupts=self._interrupts, state=None)
        return SimpleNamespace(values={}, interrupts=self._interrupts, tasks=[task])


def _scripted_chunks() -> list[tuple[str, Any]]:
    reasoning_and_call = AIMessage(
        content=[{"type": "thinking", "thinking": "let me plan"}],
        tool_calls=[{"name": "task", "args": {"description": "sub"}, "id": "call_1", "type": "tool_call"}],
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        response_metadata={"model_name": "scripted"},
    )
    tool_result = ToolMessage(content="sub-result", name="task", tool_call_id="call_1", status="success")
    return [
        ("updates", {"agent": {"messages": [reasoning_and_call]}}),
        ("updates", {"tools": {"messages": [tool_result]}}),
        ("messages", (AIMessageChunk(content="Hello "), {})),
        ("messages", (AIMessageChunk(content="world"), {})),
        ("updates", {"agent": {"structured_response": {"answer": "ok"}}}),
    ]


def _install_fake_resolve(monkeypatch: pytest.MonkeyPatch, agent: DeepAgent, graph: _FakeCompiledGraph) -> None:
    """Point the agent's ``_resolve_and_build`` at a scripted compiled graph, so
    ``run`` drains the fake through the shared streaming core with no live LLM."""

    async def fake_resolve_and_build(**_: Any) -> _FakeCompiledGraph:
        return graph

    monkeypatch.setattr(agent, "_resolve_and_build", fake_resolve_and_build)


def _install_fake_graph(monkeypatch: pytest.MonkeyPatch, agent: DeepAgent, graph: _CompiledGraphLike) -> None:
    async def fake_build_agent(**kwargs: Any) -> tuple[_CompiledGraphLike, dict[str, Any]]:
        return graph, {"configurable": {"thread_id": "t"}}

    monkeypatch.setattr(agent, "_build_agent", fake_build_agent)


def _drain_astream(gen: Any) -> None:
    async def drain() -> None:
        async for _ in gen:
            pass

    asyncio.run(drain())
