"""Cross-driver chain routing: a tool dispatched INSIDE an agent turn captures a CHAINED resume.

A tool an agent dispatches is a STEP of the agent's turn. The door's out-of-band delivery address
rides ``_park_completion`` and flows DOWN unchanged through every dispatch — the platform delivers
each run's outcome to its own stored address, so no nested driver can hijack the agent's answer.
What a nested dispatch binds is the cross-driver CHAIN routing on ``chained_resume``: a park-capable
run binds a fresh chain naming the CALL (so a nested run that parks re-enters this loop with its
terminal), and a run that cannot park binds ``None`` (so a nested run captures no stale chain).

Both halves are pinned here over real graphs — the fresh ``tools_agent`` turn and the
``langchain_deep_agent`` RESUME drive. The per-agent tool-list seams each carry their own pin
beside their own suite (see ``tests/_delivery_scope.py``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    ChainedResume,
    get_chained_resume,
    get_park_completion,
    get_resume_continuation_tool,
    is_chained_park_key,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
    suspended_interaction_marker,
)
from tai42_contract.template import TemplatedText
from tai42_contract.tools import tool_call_frame

from tai42_agents import tools_agent as tools_mod
from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.nested_dispatch import nested_tool_dispatch, scope_nested_dispatch
from tai42_agents._internal.park import AGENT_RESUME_TOOL_NAME, CHAINED_PARK_DELIVERY_TOOL_NAME, agent_resume
from tai42_agents._internal.park import capability as cap
from tai42_agents._internal.park import index as idx
from tai42_agents.langchain_deep_agent import agent as deep_mod

_COMPLETION_TOOL = "conversation_deliver"
_THREAD = "bridge:acme:alice"
_CONTEXT = {"thread_id": _THREAD}


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
    checkpoint_ttl_minutes = None
    store = "memory"
    store_conn_string = None


class _LlmSettings:
    def with_fallbacks(self, kwargs: Any) -> dict[str, Any]:
        return {}


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_park_client() -> AsyncIterator[Any]:
        yield redis

    settings = SimpleNamespace(redis_url="redis://fake")
    monkeypatch.setattr(idx, "_park_client", fake_park_client)
    monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: settings)
    monkeypatch.setattr(cap, "agents_park_redis_settings", lambda: settings)
    return redis


def _wire(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(base_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(base_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(base_mod, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(base_mod, "llm_settings", _LlmSettings)
    monkeypatch.setattr(base_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(tools_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(cap, "llm_provider_settings", _ProviderSettings)


def _agent() -> tools_mod.ToolsAgent:
    agent = tai42_app.agents.get_agent("tools_agent")
    assert isinstance(agent, tools_mod.ToolsAgent)
    return agent


class _NestedDriverTool:
    """A tool standing in for a FOREIGN parking driver reached through the agent (a flow preset,
    say): its body reads the chain routing (and the door address) exactly as such a driver would
    when deciding how it re-enters the waiting agent at its terminal."""

    def __init__(self) -> None:
        self.chain_seen: list[ChainedResume | None] = []
        self.door_seen: list[tuple[str | None, Any]] = []

    def tool(self) -> StructuredTool:
        def peek() -> str:
            self.chain_seen.append(get_chained_resume())
            self.door_seen.append(get_park_completion())
            return "peeked"

        return StructuredTool.from_function(peek, name="peek", description="A nested driver's tool.")


class _ParkingAsk:
    """The agent's OWN async ask: parks the run on a fresh interaction id."""

    def __init__(self, interaction_id: str, expiry_at: datetime | None = None) -> None:
        self._interaction_id = interaction_id
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            if get_resume_continuation_tool() is None:
                raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
            return suspended_interaction_marker(self._interaction_id, self._expiry_at, get_resume_continuation_tool())

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _call(call_id: str, name: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": {}}])


def _assert_chain_routing(seen: ChainedResume | None) -> str:
    """Assert a nested dispatch captured a CHAINED routing addressing the chain-delivery tool, and return its key.

    The chain routing is what a nested parking driver captures: it names the tool that re-enters
    the waiting agent and a fresh chained key naming this call — never the door's address, which
    flows down separately on ``_park_completion``.
    """
    assert isinstance(seen, ChainedResume)
    assert seen.delivery_tool == CHAINED_PARK_DELIVERY_TOOL_NAME
    assert is_chained_park_key(seen.chain_key)
    return seen.chain_key


def test_nested_tool_captures_a_chain_while_the_agent_park_is_outermost(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    nested = _NestedDriverTool()
    model = ScriptedChatModel([_call("c1", "peek"), _call("c2", "ask"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["peek"] = nested.tool()
    app_tools.client_tools["ask"] = _ParkingAsk("i1").tool()

    agent = _agent()

    async def go() -> None:
        token = set_park_completion(_COMPLETION_TOOL, _CONTEXT)
        try:
            # The door opens the outermost minting frame around the drive (reading the bound
            # completion as the run's out-of-band address), exactly as a live door does.
            with tool_call_frame(name="agent"):
                async for _event in agent.astream(
                    tool_names=["peek", "ask"],
                    checkpoint_provider="redis",
                    user_message=TemplatedText(content="go"),
                    thread_id=_THREAD,
                ):
                    pass
        finally:
            reset_park_completion(token)

        # The nested driver ran under a CHAINED routing (it can re-enter this agent at its terminal
        # through the chain-delivery tool), while the door's out-of-band address flowed DOWN to it
        # unchanged — the platform, not the nested driver, delivers the agent's own answer there.
        assert len(nested.chain_seen) == 1
        _assert_chain_routing(nested.chain_seen[0])
        assert nested.door_seen[0] == (_COMPLETION_TOOL, _CONTEXT)

        # The agent's own park, raised outside any tool body, is OUTERMOST: it captured no chain
        # (its terminal fires nothing; the platform delivers to the run's own address).
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["completion_tool"] is None
        assert entry["completion_context"] is None

    asyncio.run(go())


def test_nested_tool_captures_a_chain_on_the_deep_agent_resume_drive(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    """The RESUME seam carries the rule too. A resume drive rebinds the captured routing so a
    re-park keeps its chain, and the deep agent rebuilds its tool list inside that drive — so a
    tool dispatched after the answer arrives captures a fresh chain of its own, not the run's."""
    saver = InMemorySaver()
    store = InMemoryStore()
    nested = _NestedDriverTool()
    model = ScriptedChatModel([_call("c1", "ask"), _call("c2", "peek"), AIMessage(content="all done")])

    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(deep_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(deep_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(deep_mod, "store_registry", lambda: _Registry(store))
    monkeypatch.setattr(deep_mod, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(deep_mod, "llm_settings", _LlmSettings)
    monkeypatch.setattr(deep_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(cap, "llm_provider_settings", _ProviderSettings)

    app_tools.client_tools["peek"] = nested.tool()
    app_tools.client_tools["ask"] = _ParkingAsk("i1", expiry_at=datetime.now(UTC) + timedelta(hours=1)).tool()

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> None:
        token = set_park_completion(_COMPLETION_TOOL, _CONTEXT)
        try:
            # The door opens the outermost minting frame around the drive (reading the bound
            # completion as the run's out-of-band address), exactly as a live door does.
            with tool_call_frame(name="agent"):
                async for _event in agent.astream(
                    tool_names=["peek", "ask"],
                    checkpoint_provider="redis",
                    user_message=TemplatedText(content="go"),
                    thread_id=_THREAD,
                ):
                    pass
        finally:
            reset_park_completion(token)

        assert await agent_resume("i1", "the answer") == "all done"
        # The tool dispatched DURING the resume drive captured a chained routing of its own.
        assert len(nested.chain_seen) == 1
        _assert_chain_routing(nested.chain_seen[0])

    asyncio.run(go())


def test_the_chain_is_reset_after_a_nested_dispatch_and_the_door_flows_through() -> None:
    # Scoped to the dispatch ONLY: the ambient chain is restored on both sides, and the door's
    # out-of-band address is never touched — it flows down unchanged.
    resume = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
    token = set_park_completion(_COMPLETION_TOOL, _CONTEXT)
    try:
        assert get_chained_resume() is None
        with nested_tool_dispatch(chain=True):
            _assert_chain_routing(get_chained_resume())
            # The door's address flows down unchanged inside the dispatch.
            assert get_park_completion() == (_COMPLETION_TOOL, _CONTEXT)
        assert get_chained_resume() is None
        assert get_park_completion() == (_COMPLETION_TOOL, _CONTEXT)
    finally:
        reset_park_completion(token)
        reset_resume_continuation_tool(resume)


def test_a_chained_dispatch_needs_a_run_that_can_park() -> None:
    # The chain is honored only where this run could actually park on the call. With no resume
    # continuation bound — a run that cannot park — a chain would name a key nothing will ever park
    # on, so ``chained_resume`` is cleared to None and the nested park is refused downstream.
    with nested_tool_dispatch(chain=True):
        assert get_chained_resume() is None


def test_a_chained_dispatch_binds_a_fresh_key_per_call() -> None:
    # One chained key names one CALL, so two dispatches are two keys: the terminals of two nested
    # runs can never converge on one park.
    resume = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
    try:
        keys = []
        for _ in range(2):
            with nested_tool_dispatch(chain=True):
                keys.append(_assert_chain_routing(get_chained_resume()))
        assert keys[0] != keys[1]
        assert get_chained_resume() is None
    finally:
        reset_resume_continuation_tool(resume)


def test_a_chained_dispatch_carries_the_ancestors_call_chain() -> None:
    # The routing carries the ANCESTOR's own call chain, passed as ``continues_chain`` on the chain
    # re-entry so the ancestor's re-park records its own chain, not the descendant's plus the chain
    # tool's name. Here the ambient chain is empty (no ``run_tool`` frame), so ``asked_by`` is ().
    resume = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
    try:
        with nested_tool_dispatch(chain=True):
            routing = get_chained_resume()
            assert isinstance(routing, ChainedResume)
            assert routing.asked_by == ()
    finally:
        reset_resume_continuation_tool(resume)


def test_scoping_preserves_the_model_facing_tool_surface() -> None:
    # A body swap only: the name, description and advertised argument schema an agent's model
    # selects on are untouched, so scoping can be applied to the whole resolved tool list.
    def echo(text: str) -> str:
        """Echo the text."""
        return text

    original = StructuredTool.from_function(echo, name="echo", description="Echo the text.")
    scoped = scope_nested_dispatch(original)

    assert scoped is not original
    assert scoped.name == original.name
    assert scoped.description == original.description
    assert scoped.args == original.args
    assert scoped.func is not None
    assert scoped.func("hi") == "hi"
    # The ORIGINAL is left alone: ``get_client_tools`` builds these over the shared registry and a
    # caller may hand one live tool to several agents, so scoping must never mutate in place.
    assert original.func is not None
    assert original.func is not scoped.func


def test_scoping_clears_the_chain_through_an_async_body() -> None:
    # The async half carries the SAME clearing property as the sync half — most client tools are
    # coroutine-bodied, so asserting only the sync path would leave the common case unpinned.
    # Outside a park-capable drive the dispatch binds no chain, so the body sees ``chained_resume``
    # cleared to None.
    seen: list[ChainedResume | None] = []

    async def peek() -> str:
        """Peek at the chain."""
        seen.append(get_chained_resume())
        return "peeked"

    scoped = scope_nested_dispatch(
        StructuredTool.from_function(func=None, coroutine=peek, name="peek", description="Peek at the chain.")
    )

    async def drive() -> Any:
        assert scoped.coroutine is not None
        return await scoped.coroutine()

    assert asyncio.run(drive()) == "peeked"
    assert seen == [None]


def test_a_bodyless_tool_passes_through_with_a_warning(caplog) -> None:
    # A plain ``BaseTool`` subclass implements ``_run``/``_arun`` and carries NEITHER ``func`` nor
    # ``coroutine``: reading them directly raises, and this is the graceful path. It dispatches
    # UNSCOPED, which is an ownership hole, so it is announced — never silently accepted, and
    # never an exception that would take down a host whose tool works fine today.
    class _PlainTool(BaseTool):
        name: str = "plain"
        description: str = "a bodyless tool"

        def _run(self, *args: Any, **kwargs: Any) -> str:
            return "ran"

    plain = _PlainTool()
    with caplog.at_level(logging.WARNING, logger="tai42_agents._internal.nested_dispatch"):
        out = scope_nested_dispatch(plain)

    assert out is plain
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "'plain'" in warnings[0]
