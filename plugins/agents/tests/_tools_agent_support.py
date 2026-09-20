"""Shared rig for the ``tools_agent`` test modules: the agent lookup, a tool
factory, config-capture and astream-drain helpers, and the structured-format
schema the run and astream faces both assert against.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
from langchain_core.tools import StructuredTool
from tai42_contract.agent import (
    Agent,
    MessageFinal,
    StreamEvent,
)
from tai42_contract.app import tai42_app

from tai42_agents import tools_agent as tools_agent_module
from tai42_agents._internal.usage import AgentInvokeResult, CallUsage
from tai42_agents.tools_agent import ToolsAgent

AGENT_NAME = "tools_agent"


def make_tool(name: str, props: dict[str, Any] | None = None) -> StructuredTool:
    async def call_tool(**kwargs: Any) -> Any:
        return kwargs

    return StructuredTool.from_function(
        func=None,
        coroutine=call_tool,
        name=name,
        description="t",
        args_schema={"type": "object", "properties": props or {}, "required": []},
    )


def _get_agent() -> ToolsAgent:
    agent = tai42_app.agents.get_agent(AGENT_NAME)
    assert isinstance(agent, ToolsAgent)
    return agent


_STRUCTURED_SCHEMA = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}


def _capture_config(monkeypatch: pytest.MonkeyPatch, face: str, **kwargs: Any) -> dict[str, Any]:
    """Drive one face with ``kwargs`` against a scripted seam and return the run
    config that seam received, so a single assertion body can be run against both
    faces and prove they agree."""
    captured: dict[str, Any] = {}
    agent = _get_agent()

    if face == "run":

        async def fake_invoke(**seen: Any) -> AgentInvokeResult:
            captured.update(seen)
            return AgentInvokeResult(output="ok", usage=CallUsage(0, 0, None))

        monkeypatch.setattr(tools_agent_module, "ainvoke_tools_agent", fake_invoke)
        asyncio.run(agent.run(**kwargs))
    else:
        _script_astream(monkeypatch, [MessageFinal(text="ok")], captured)
        _collect(agent, **kwargs)
    return captured["config"]


def _script_astream(monkeypatch: pytest.MonkeyPatch, events: list[StreamEvent], captured: dict[str, Any]) -> None:
    async def fake_events(**kwargs: Any) -> AsyncIterator[StreamEvent]:
        captured.update(kwargs)
        for event in events:
            yield event

    monkeypatch.setattr(tools_agent_module, "astream_tools_agent_events", fake_events)


def _collect(agent: Agent, **kwargs: Any) -> list[StreamEvent]:
    async def go() -> list[StreamEvent]:
        return [event async for event in agent.astream(**kwargs)]

    return asyncio.run(go())
