"""A caller ask behind a preset parks the ``tools_agent`` as a CALLER ask.

The preset adapter turns its base tool's park into the reserved park marker; the marker must
carry both id lists, so the agent's ``SuspendedFinal`` (and the drained receipt) name the caller
subset and the platform's visit classifies the park as caller asks, never a silent user park.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from tai42_contract.agent.base import PresetSpec
from tai42_contract.agent.events import SuspendedFinal
from tai42_contract.interactions import (
    SuspendedInteraction,
    get_resume_continuation_tool,
    read_suspended_interaction_marker,
)
from tai42_contract.template import TemplatedText
from tai42_contract.tools import tool_call_frame
from tests._client_tool import client_tool
from tests._tools_agent_park_support import (
    ScriptedChatModel,
    _agent,
    _tool_messages,
    _wire_tools_build,
    _within_retention,
)
from tests.conftest import bind_park_index


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the park index at a shared in-memory fakeredis (a park-capable run)."""
    redis = aioredis.FakeRedis(decode_responses=True)
    bind_park_index(monkeypatch, redis)
    return redis


class _CallerAskBase:
    """A base tool that asks the CALLER asynchronously: it parks under the run's own resume
    continuation and names its interaction in the caller subset, as ``ask(to="caller",
    mode="async")`` does."""

    def __init__(self, interaction_id: str) -> None:
        self.calls = 0
        self.interaction_id = interaction_id
        self.expiry_at = _within_retention()

    def run(self, **_kwargs: Any) -> SuspendedInteraction:
        self.calls += 1
        return SuspendedInteraction(
            interaction_id=self.interaction_id,
            resume_owner=get_resume_continuation_tool(),
            expiry_at=self.expiry_at,
            interaction_ids=[self.interaction_id],
            caller_interaction_ids=[self.interaction_id],
        )


def _preset_call() -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": "c1", "name": "ask_caller_preset", "args": {}}])


def _preset() -> PresetSpec:
    return PresetSpec(name="ask_caller_preset", base_tool="ask_caller_base", description="Ask the caller.")


def test_a_caller_ask_behind_a_preset_parks_with_the_caller_subset(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    base = _CallerAskBase("i-caller")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unused")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask_caller_base"] = client_tool("ask_caller_base", base.run)
    app_tools.tool_runners["ask_caller_base"] = base.run

    async def go() -> None:
        # The door opened the minting call frame: the astream face is park-capable.
        with tool_call_frame(name="agent"):
            events = [
                event
                async for event in _agent().astream(
                    presets=[_preset()],
                    checkpoint_provider="redis",
                    user_message=TemplatedText(content="go"),
                    thread_id="t-preset-caller",
                )
            ]
        suspended = [event for event in events if isinstance(event, SuspendedFinal)]
        assert len(suspended) == 1
        assert suspended[0].interaction_ids == ["i-caller"]
        assert suspended[0].caller_interaction_ids == ["i-caller"]
        assert base.calls == 1

        # The checkpointed ToolMessage carries the marker with BOTH id lists.
        state = await saver.aget_tuple({"configurable": {"thread_id": "t-preset-caller"}})
        assert state is not None
        messages = state.checkpoint["channel_values"]["messages"]
        (tool_message,) = _tool_messages(messages)
        marker = read_suspended_interaction_marker(tool_message.content)
        assert marker is not None
        assert marker["interaction_ids"] == ["i-caller"]
        assert marker["caller_interaction_ids"] == ["i-caller"]

    asyncio.run(go())


def test_a_caller_ask_behind_a_preset_drains_to_a_caller_receipt(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    base = _CallerAskBase("i-caller")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unused")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask_caller_base"] = client_tool("ask_caller_base", base.run)
    app_tools.tool_runners["ask_caller_base"] = base.run

    async def go() -> None:
        receipt = await _agent().run(
            presets=[_preset()],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-preset-caller-run",
        )
        assert isinstance(receipt, SuspendedInteraction)
        assert receipt.interaction_ids == ["i-caller"]
        assert receipt.caller_interaction_ids == ["i-caller"]

    asyncio.run(go())
