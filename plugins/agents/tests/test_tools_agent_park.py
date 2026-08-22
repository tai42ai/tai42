"""The ``tools_agent`` async ``ask_user`` park/resume — parity with ``langchain_deep_agent``.

``tools_agent`` builds through ``create_agent`` (not ``create_deep_agent``); these tests
prove the SAME shared park machinery covers it: the ``run`` face parks on an async ask and
returns a suspended receipt, ``agent_resume`` rebuilds the graph on a fresh runtime (same
checkpointer + the durable park index) and drives it to completion running the parked tool
exactly once, expiry feeds the expiry marker, a non-durable/non-rebuildable run refuses,
and the ``astream`` face (no completion delivery bound) refuses an async ask loudly.

The park index is backed by an in-memory fakeredis routed through the index module's
``client_ctx`` seam; the checkpoint is an ``InMemorySaver`` shared between the park run and
the resume, standing in for the durable checkpoint a cross-worker resume reads.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    EXPIRY_ANSWER,
    get_resume_continuation_tool,
    suspended_interaction_marker,
)

from tai42_agents import tools_agent as tools_mod
from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import agent_resume
from tai42_agents._internal.park import driver as drv
from tai42_agents._internal.park import index as idx


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


class _AskStandIn:
    """A faithful ``ask_user(mode="async")`` stand-in: it parks (returns the reserved
    marker) ONLY when a resume continuation is bound, and otherwise RAISES exactly as the
    platform helper does with no driver bound — so a face that binds no resume path refuses
    loudly. Counts calls so a resume that re-ran the tool (a double-park) is caught."""

    def __init__(self, interaction_id: str, expiry_at: Any = None) -> None:
        self.calls = 0
        self._interaction_id = interaction_id
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            self.calls += 1
            if get_resume_continuation_tool() is None:
                raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
            return suspended_interaction_marker(self._interaction_id, self._expiry_at)

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _ask_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "ask", "args": {}}])


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the park index at a shared in-memory fakeredis and report the park Redis as
    configured (so a run is judged park-capable)."""
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


class _ProviderSettings:
    llm = "fake"
    checkpoint = "redis"
    checkpoint_conn_string = None
    # Keep-forever retention, so the park-persist expiry-vs-retention gate bounds nothing
    # and these resume-mechanics tests' synthetic None-expiry parks pass it.
    checkpoint_ttl_minutes = None
    store = "memory"
    store_conn_string = None


class _LlmSettings:
    def with_fallbacks(self, kwargs: Any) -> dict[str, Any]:
        return {}


def _wire_tools_build(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
    """Keep the REAL ``_compile_tools_agent`` but inject the scripted model + a SHARED
    checkpointer, so the park run and its resume read one durable checkpoint. The
    monitoring-callback wiring is stripped from the run config (the recording writer hands
    back string sentinels the graph cannot use)."""

    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(base_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(base_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(base_mod, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(base_mod, "llm_settings", _LlmSettings)
    monkeypatch.setattr(base_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(tools_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    # build_park_identity resolves the checkpoint provider through the kit settings when the
    # caller passes none; here the caller pins "redis", but pin the driver's view too so the
    # durable-provider gate is deterministic.
    monkeypatch.setattr(drv, "llm_provider_settings", _ProviderSettings)


def _agent() -> tools_mod.ToolsAgent:
    agent = tai42_app.agents.get_agent("tools_agent")
    assert isinstance(agent, tools_mod.ToolsAgent)
    return agent


def test_tools_agent_park_then_answer_exactly_once(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        receipt = await agent.run(
            tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-tools"
        )
        assert receipt == {
            "status": "suspended",
            "interaction_ids": ["i1"],
            "thread_id": "t-tools",
            "expiry_at": None,
        }
        assert ask.calls == 1
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["agent_name"] == "tools_agent"

        # Resume on a fresh runtime: same shared saver stands in for the durable checkpoint.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        # The parked tool ran exactly once — the resume substituted the answer, never re-ran it.
        assert ask.calls == 1
        # A clean drive finalizes the park entry to a resolved tombstone (not an absent key).
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_tools_agent_park_then_expiry_feeds_the_expiry_marker(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    deadline = datetime(2030, 1, 1, tzinfo=UTC)
    ask = _AskStandIn("i1", expiry_at=deadline)
    model = ScriptedChatModel([_ask_call(), AIMessage(content="expired path")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        receipt = await agent.run(
            tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-expiry"
        )
        assert receipt["expiry_at"] == deadline.isoformat()
        result = await agent_resume("i1", EXPIRY_ANSWER)
        assert result == "expired path"
        # A clean drive finalizes the park entry to a resolved tombstone (not an absent key).
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_tools_agent_park_has_no_interrupt_on_collision(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # tools_agent wires no HITL interrupt_on, so the ONLY pending interrupt a super-step can
    # raise is the async-ask park — there is nothing for it to collide with. The park resumes
    # cleanly by its own id.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        await agent.run(tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-hitl")
        entry = await idx.read_park_entry("i1")
        # Exactly one park interrupt id was stored — no second (HITL) interrupt exists.
        assert entry is not None
        assert isinstance(entry["interrupt_id"], str)
        result = await agent_resume("i1", "answer")
        assert result == "done"

    asyncio.run(go())


def test_tools_agent_run_refuses_non_durable_checkpoint(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A memory checkpoint is not park-capable: the async ask has no resume path bound, so the
    # ask stand-in RAISES loudly pre-persist rather than parking with nowhere to resume.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        with pytest.raises(Exception, match="resuming driver"):
            await agent.run(
                tool_names=["ask"], checkpoint_provider="memory", user_message="go", thread_id="t-nondurable"
            )
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())


def test_tools_agent_run_refuses_live_tools(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A run carrying a LIVE tool is not rebuildable from names, so it is not park-capable:
    # the resume continuation is not bound and the async ask refuses loudly pre-persist.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)

    agent = _agent()

    async def go() -> None:
        with pytest.raises(Exception, match="resuming driver"):
            await agent.run(tools=[ask.tool()], checkpoint_provider="redis", user_message="go", thread_id="t-livetools")
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())


def test_tools_agent_astream_refuses_async_ask_without_completion(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The astream face binds NO resume path when no completion tool is bound (a direct SSE
    # run), so an async ask refuses loudly pre-persist — the option-ii boundary is unchanged.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        with pytest.raises(Exception, match="resuming driver"):
            async for _event in agent.astream(
                tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-astream"
            ):
                pass
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())
