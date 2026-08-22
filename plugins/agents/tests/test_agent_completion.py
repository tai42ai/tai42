"""The completion-continuation: a resumed run's FINAL answer delivered out of band.

When a completion tool is bound around a park-capable ``astream`` run, the park entry stores
it and a CLEAN TERMINAL drive AWAITS ``run_tool(completion_tool, {thread_id, result,
completion_id})`` with the final answer BEFORE finalizing the index, so the durable delivery
commit precedes the tombstone. A re-park carries the completion tool forward to the new entry
(fired only when the run finally terminates). A handoff failure raises loudly and leaves the
index LIVE, so the platform redelivers and re-drives — the completion is never dropped.

Driven over a real ``tools_agent`` / ``langchain_deep_agent`` graph with a scripted model + shared
in-memory checkpointer and the fakeredis-backed park index (same wiring as the park tests).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    get_resume_continuation_tool,
    reset_park_completion_tool,
    set_park_completion_tool,
    suspended_interaction_marker,
)

from tai42_agents import tools_agent as tools_mod
from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import agent_resume
from tai42_agents._internal.park import driver as drv
from tai42_agents._internal.park import index as idx
from tai42_agents.langchain_deep_agent import agent as agent_mod

_COMPLETION_TOOL = "conversation_deliver"


class ScriptedChatModel(BaseChatModel):
    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        message = self._responses[self._index]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


# Within the durable-workspace retention horizon (session_ttl, 24h default) — the deep agent's
# parks must carry a deadline inside it.
_WITHIN_HORIZON = datetime.now(UTC) + timedelta(hours=1)


class _SequentialAsk:
    """A tool that parks on a fresh interaction id each call (so a run can park, resume, then
    re-park on a second ask). Refuses loudly if no resume continuation is bound."""

    def __init__(self, ids: list[str], expiry_at: datetime | None = None) -> None:
        self.calls = 0
        self._ids = ids
        # The durable ``langchain_deep_agent`` acquires a persistent workspace whose TTL bounds
        # the park retention to min(checkpoint, workspace) (§B3.1), so its parks must carry an ask
        # deadline within that horizon; the non-durable ``tools_agent`` keeps a keep-forever
        # (None) retention and its parks pass with no deadline.
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            if get_resume_continuation_tool() is None:
                raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
            interaction_id = self._ids[self.calls]
            self.calls += 1
            return suspended_interaction_marker(interaction_id, self._expiry_at)

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _ask_call(call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "ask", "args": {}}])


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_park_client() -> AsyncIterator[Any]:
        yield redis

    settings = SimpleNamespace(redis_url="redis://fake")
    monkeypatch.setattr(idx, "_park_client", fake_park_client)
    monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: settings)
    monkeypatch.setattr(drv, "agents_park_redis_settings", lambda: settings)
    return redis


class _Registry:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def get_checkpointer(self, **kwargs: Any) -> Any:
        return self._value

    async def get_store(self, **kwargs: Any) -> Any:
        return self._value


class _ProviderSettings:
    llm = "fake"
    checkpoint = "redis"
    checkpoint_conn_string = None
    # Keep-forever retention, so the park-persist expiry-vs-retention gate bounds nothing
    # and this resume-mechanics test's synthetic None-expiry park passes it.
    checkpoint_ttl_minutes = None
    store = "memory"
    store_conn_string = None


class _LlmSettings:
    def with_fallbacks(self, kwargs: Any) -> dict[str, Any]:
        return {}


def _wire(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(base_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(base_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(base_mod, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(base_mod, "llm_settings", _LlmSettings)
    monkeypatch.setattr(base_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(tools_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(drv, "llm_provider_settings", _ProviderSettings)


def _agent() -> tools_mod.ToolsAgent:
    agent = tai42_app.agents.get_agent("tools_agent")
    assert isinstance(agent, tools_mod.ToolsAgent)
    return agent


async def _park_via_astream(agent: tools_mod.ToolsAgent, thread_id: str) -> None:
    """Drive a fresh turn through the astream face with the completion tool bound, so the
    run parks with a completion delivery path recorded on its park entry."""
    token = set_park_completion_tool(_COMPLETION_TOOL)
    try:
        async for _event in agent.astream(
            tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id=thread_id
        ):
            pass
    finally:
        reset_park_completion_tool(token)


def _expected_delivery(thread_id: str, interaction_ids: list[str], result: Any) -> dict[str, Any]:
    """The exact kwargs a clean-terminal completion handoff fires: the parked thread, the final
    answer, and the stable completion id derived from (thread_id, super-step of the resolved
    interactions)."""
    completion_id = drv._completion_id(thread_id, idx.compute_superstep_id(interaction_ids))
    return {"thread_id": thread_id, "result": result, "completion_id": completion_id}


def test_completion_fires_on_terminal_drive(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["completion_tool"] == _COMPLETION_TOOL

        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        # The completion tool fired ONCE, awaited, with the parked thread, the final answer, and
        # the stable completion id.
        assert delivered == [_expected_delivery("bridge:acme:alice", ["i1"], "all done")]
        # A clean drive finalizes the entry to a resolved tombstone AFTER the completion handoff.
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_completion_carried_forward_on_repark(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1", "i2"])
    model = ScriptedChatModel([_ask_call("c1"), _ask_call("c2"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")

        # First resume RE-PARKS on the second ask; the completion tool is carried forward to
        # the new entry, and is NOT fired on a re-park.
        receipt = await agent_resume("i1", "a1")
        assert receipt["status"] == "suspended"
        assert receipt["interaction_ids"] == ["i2"]
        assert delivered == []
        new_entry = await idx.read_park_entry("i2")
        assert new_entry is not None
        assert new_entry["completion_tool"] == _COMPLETION_TOOL

        # Second resume terminates; the completion fires once with the final answer, keyed by the
        # SECOND super-step (the one that resolved).
        result = await agent_resume("i2", "a2")
        assert result == "all done"
        assert delivered == [_expected_delivery("bridge:acme:alice", ["i2"], "all done")]

    asyncio.run(go())


def _wire_deep(
    monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver, store: InMemoryStore
) -> None:
    """Keep the REAL build_langchain_deep_agent but inject the scripted model + a SHARED
    checkpointer/store, so a langchain_deep_agent park run and its resume read one durable checkpoint."""

    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(agent_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(agent_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(agent_mod, "store_registry", lambda: _Registry(store))
    monkeypatch.setattr(agent_mod, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(agent_mod, "llm_settings", _LlmSettings)
    monkeypatch.setattr(agent_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(drv, "llm_provider_settings", _ProviderSettings)


async def _park_via_astream_deep(agent: Any, thread_id: str) -> None:
    """Drive a fresh langchain_deep_agent turn through the astream face with the completion tool bound,
    so the run parks with a completion delivery path recorded on its park entry."""
    token = set_park_completion_tool(_COMPLETION_TOOL)
    try:
        async for _event in agent.astream(
            tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id=thread_id
        ):
            pass
    finally:
        reset_park_completion_tool(token)


def test_langchain_deep_agent_completion_carried_forward_on_repark(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    store = InMemoryStore()
    ask = _SequentialAsk(["i1", "i2"], expiry_at=_WITHIN_HORIZON)
    model = ScriptedChatModel([_ask_call("c1"), _ask_call("c2"), AIMessage(content="all done")])
    _wire_deep(monkeypatch, model, saver, store)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> None:
        await _park_via_astream_deep(agent, "bridge:acme:alice")

        # First resume RE-PARKS on the second ask; the completion tool must be carried forward
        # to the new entry (the langchain_deep_agent resume face rebinds it), and is NOT fired on a re-park.
        receipt = await agent_resume("i1", "a1")
        assert receipt["status"] == "suspended"
        assert receipt["interaction_ids"] == ["i2"]
        assert delivered == []
        new_entry = await idx.read_park_entry("i2")
        assert new_entry is not None
        assert new_entry["completion_tool"] == _COMPLETION_TOOL

        # Second resume terminates; the completion fires once with the final answer, keyed by the
        # SECOND super-step (the one that resolved).
        result = await agent_resume("i2", "a2")
        assert result == "all done"
        assert delivered == [_expected_delivery("bridge:acme:alice", ["i2"], "all done")]

    asyncio.run(go())


def test_langchain_deep_agent_completion_handoff_crash_then_redelivery_delivers_once(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    store = InMemoryStore()
    ask = _SequentialAsk(["i1"], expiry_at=_WITHIN_HORIZON)
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire_deep(monkeypatch, model, saver, store)
    app_tools.client_tools["ask"] = ask.tool()

    calls: list[dict[str, Any]] = []

    def _flaky(**kwargs: Any) -> None:
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("delivery backend down")

    app_tools.tool_runners[_COMPLETION_TOOL] = _flaky

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> None:
        await _park_via_astream_deep(agent, "bridge:acme:alice")
        superstep_id = idx.compute_superstep_id(["i1"])

        with pytest.raises(RuntimeError, match="delivery backend down"):
            await agent_resume("i1", "the answer")
        assert not idx.is_resolved_tombstone(await idx.read_park_entry("i1") or {})
        await fake_park_redis.delete(idx._claim_key("bridge:acme:alice", superstep_id))

        # Redelivery re-drives the already-terminal graph idempotently (re-produced from the
        # persisted state, never re-invoked) and re-fires the handoff under the SAME stable id.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        assert [c["completion_id"] for c in calls] == [
            drv._completion_id("bridge:acme:alice", superstep_id),
            drv._completion_id("bridge:acme:alice", superstep_id),
        ]
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_completion_handoff_failure_raises_and_leaves_index_live(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("delivery backend down")

    app_tools.tool_runners[_COMPLETION_TOOL] = _boom

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")
        # The drive succeeds, but the completion handoff is AWAITED before finalize: its failure
        # raises out of the resume and the index is NOT finalized, so the platform keeps the
        # due-record and a redelivery re-drives and re-reaches the handoff.
        with pytest.raises(RuntimeError, match="delivery backend down"):
            await agent_resume("i1", "the answer")
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert not idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_completion_handoff_crash_then_redelivery_delivers_once(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    calls: list[dict[str, Any]] = []

    def _flaky(**kwargs: Any) -> None:
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("delivery backend down")

    app_tools.tool_runners[_COMPLETION_TOOL] = _flaky

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")
        superstep_id = idx.compute_superstep_id(["i1"])

        # First drive succeeds, handoff crashes → resume raises, index left LIVE, no finalize.
        with pytest.raises(RuntimeError, match="delivery backend down"):
            await agent_resume("i1", "the answer")
        assert not idx.is_resolved_tombstone(await idx.read_park_entry("i1") or {})
        # The crashed winner's lease lapses (simulated), so a redelivery reclaims and re-drives.
        await fake_park_redis.delete(idx._claim_key("bridge:acme:alice", superstep_id))

        # Redelivery re-drives idempotently and re-reaches the handoff, which now succeeds.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        # Both drives fired the handoff under the SAME stable completion id, so a delivery
        # ledger keyed on that id collapses the redelivery to a single record.
        assert [c["completion_id"] for c in calls] == [
            drv._completion_id("bridge:acme:alice", superstep_id),
            drv._completion_id("bridge:acme:alice", superstep_id),
        ]
        # The successful drive finalized the entry to a resolved tombstone.
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())
