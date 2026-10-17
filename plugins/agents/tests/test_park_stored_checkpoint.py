"""``require_stored_checkpoint``: a resume proceeds only over a checkpoint the store actually holds."""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from tai42_agents._internal.park import require_stored_checkpoint
from tai42_agents._internal.park.errors import AgentResumeCheckpointMissingError


class _State(TypedDict):
    messages: Annotated[list[str], operator.add]


def _graph() -> Any:
    builder = StateGraph(_State)
    builder.add_node("echo", lambda state: {"messages": ["seen"]})
    builder.add_edge(START, "echo")
    builder.add_edge("echo", END)
    return builder.compile(checkpointer=InMemorySaver())


def test_a_thread_with_no_checkpoint_is_refused() -> None:
    async def go() -> None:
        snapshot = await _graph().aget_state({"configurable": {"thread_id": "none"}}, subgraphs=True)
        with pytest.raises(AgentResumeCheckpointMissingError):
            require_stored_checkpoint(snapshot, "none")

    asyncio.run(go())


def test_a_pinned_checkpoint_id_the_store_does_not_hold_is_refused() -> None:
    # The snapshot of a missing checkpoint echoes the pinned id back in its config; only the
    # stored checkpoint itself counts.
    async def go() -> None:
        config = {"configurable": {"thread_id": "none", "checkpoint_id": "1f000000-0000-0000-0000-000000000000"}}
        snapshot = await _graph().aget_state(config, subgraphs=True)
        with pytest.raises(AgentResumeCheckpointMissingError):
            require_stored_checkpoint(snapshot, "none")

    asyncio.run(go())


def test_a_stored_checkpoint_passes_latest_or_pinned() -> None:
    async def go() -> None:
        graph = _graph()
        config: dict[str, Any] = {"configurable": {"thread_id": "t"}}
        await graph.ainvoke({"messages": ["hi"]}, config)
        latest = await graph.aget_state(config, subgraphs=True)
        require_stored_checkpoint(latest, "t")
        pinned = {"configurable": {"thread_id": "t", "checkpoint_id": latest.config["configurable"]["checkpoint_id"]}}
        require_stored_checkpoint(await graph.aget_state(pinned, subgraphs=True), "t")

    asyncio.run(go())
