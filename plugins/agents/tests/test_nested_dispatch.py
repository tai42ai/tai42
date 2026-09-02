"""Cross-driver delivery ownership: a tool dispatched INSIDE an agent turn cannot capture the
agent's completion binding.

The park-completion binding is the deferred-answer DELIVERY ADDRESS of the interaction the
conversation door is waiting on, and it rides a contextvar. Without scoping, a parking driver
reached through a tool — a flow preset invoked as a tool, say — reads that address as its own,
posts its raw envelope into the guest thread, and orphans the agent's real answer.

The owner of the interaction owns delivery: a tool an agent dispatches never runs under the
door's binding, while the agent's own park (raised by the graph, outside any tool body) still
captures the binding the door set for it. What the dispatch binds INSTEAD depends on whether
this run can park: a park-capable run binds a CHAINED completion addressing the CALL (so a
nested run that parks is waited on rather than orphaned), and a run that cannot park binds
nothing at all. Either way the door's address is unreachable from inside the tool — the chained
context EMBEDS it rather than exposing it.

Both halves are pinned here over real graphs — the fresh ``tools_agent`` turn and the
``langchain_deep_agent`` RESUME drive, where the driver rebinds the stored completion so a
re-park keeps delivering. The per-agent tool-list seams each carry their own pin beside their
own suite (see ``tests/_delivery_scope.py``).
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
    CHAINED_PARK_CONTEXT_KEY,
    CHAINED_PARK_TOKEN_KEY,
    PARK_COMPLETION_THREAD_KEY,
    get_park_completion,
    get_resume_continuation_tool,
    is_chained_park_key,
    reset_park_completion,
    reset_resume_continuation_tool,
    set_park_completion,
    set_resume_continuation_tool,
    suspended_interaction_marker,
)

from tai42_agents import tools_agent as tools_mod
from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.nested_dispatch import nested_tool_dispatch, scope_nested_dispatch
from tai42_agents._internal.park import AGENT_RESUME_TOOL_NAME, CHAINED_PARK_DELIVERY_TOOL_NAME, agent_resume
from tai42_agents._internal.park import driver as drv
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
    monkeypatch.setattr(drv, "agents_park_redis_settings", lambda: settings)
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
    monkeypatch.setattr(drv, "llm_provider_settings", _ProviderSettings)


def _agent() -> tools_mod.ToolsAgent:
    agent = tai42_app.agents.get_agent("tools_agent")
    assert isinstance(agent, tools_mod.ToolsAgent)
    return agent


class _NestedDriverTool:
    """A tool standing in for a FOREIGN parking driver reached through the agent (a flow preset,
    say): its body reads the completion binding exactly as such a driver would when deciding
    whether it has a delivery path of its own."""

    def __init__(self) -> None:
        self.seen: list[tuple[str | None, Any]] = []

    def tool(self) -> StructuredTool:
        def peek() -> str:
            self.seen.append(get_park_completion())
            return "peeked"

        return StructuredTool.from_function(peek, name="peek", description="A nested driver's tool.")


class _ParkingAsk:
    """The agent's OWN async ask: parks the run on a fresh interaction id."""

    def __init__(self, interaction_id: str, expiry_at: datetime | None = None) -> None:
        self._interaction_id = interaction_id
        # The durable deep agent bounds park retention to its workspace TTL, so its parks must
        # carry a deadline inside that horizon; the non-durable tools_agent needs none.
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            if get_resume_continuation_tool() is None:
                raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
            return suspended_interaction_marker(self._interaction_id, self._expiry_at, get_resume_continuation_tool())

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _call(call_id: str, name: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": name, "args": {}}])


def _assert_chained_over(seen: tuple[str | None, Any], wrapped: tuple[str | None, Any]) -> str:
    """Assert a nested dispatch saw a CHAINED binding wrapping ``wrapped``, and return its key.

    The chained binding is what a nested parking driver reads: it addresses the chain's delivery
    tool (which re-enters the waiting agent), never the door's — the door's binding survives
    EMBEDDED, so nothing is lost and nothing is reachable."""
    tool, context = seen
    assert tool == CHAINED_PARK_DELIVERY_TOOL_NAME
    assert context is not None
    key = context[CHAINED_PARK_TOKEN_KEY]
    assert is_chained_park_key(key)
    wrapped_tool, wrapped_context = wrapped
    assert context[CHAINED_PARK_CONTEXT_KEY] == {"tool": wrapped_tool, "context": wrapped_context}
    return key


def test_nested_tool_sees_no_binding_while_the_agent_park_still_captures(
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
            async for _event in agent.astream(
                tool_names=["peek", "ask"], checkpoint_provider="redis", user_message="go", thread_id=_THREAD
            ):
                pass
        finally:
            reset_park_completion(token)

        # The nested driver ran under the CHAINED binding: it can neither fire the conversation
        # door's delivery tool nor read the guest thread it addresses — that binding survives
        # only embedded inside the chain's context — and what it CAN fire re-enters this agent.
        assert len(nested.seen) == 1
        _assert_chained_over(nested.seen[0], (_COMPLETION_TOOL, _CONTEXT))

        # The agent's own park, raised outside any tool body, captured the binding normally, so
        # its resumed answer still has its delivery path.
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["completion_tool"] == _COMPLETION_TOOL
        assert entry["completion_context"] == _CONTEXT

    asyncio.run(go())


def test_nested_tool_sees_no_binding_on_the_deep_agent_resume_drive(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    """The RESUME seam carries the rule too. A resume drive REBINDS the stored completion so a
    re-park keeps delivering, and the deep agent rebuilds its tool list inside that drive — so
    without scoping a tool dispatched after the answer arrives would read the rebound address."""
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
    monkeypatch.setattr(drv, "llm_provider_settings", _ProviderSettings)

    app_tools.client_tools["peek"] = nested.tool()
    app_tools.client_tools["ask"] = _ParkingAsk("i1", expiry_at=datetime.now(UTC) + timedelta(hours=1)).tool()
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: None

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> None:
        token = set_park_completion(_COMPLETION_TOOL, _CONTEXT)
        try:
            async for _event in agent.astream(
                tool_names=["peek", "ask"], checkpoint_provider="redis", user_message="go", thread_id=_THREAD
            ):
                pass
        finally:
            reset_park_completion(token)

        assert await agent_resume("i1", "the answer") == "all done"
        # The tool dispatched DURING the resume drive saw the chained binding, not the stored
        # completion the drive rebound around it for the re-park path.
        assert len(nested.seen) == 1
        _assert_chained_over(nested.seen[0], (_COMPLETION_TOOL, _CONTEXT))

    asyncio.run(go())


def test_the_binding_is_restored_after_a_nested_dispatch() -> None:
    # Scoped to the dispatch ONLY: the caller's own binding is intact on both sides of it, so a
    # tool call never costs the agent the delivery path bound for its own answer.
    token = set_park_completion(_COMPLETION_TOOL, _CONTEXT)
    try:
        assert get_park_completion() == (_COMPLETION_TOOL, _CONTEXT)
        with nested_tool_dispatch():
            assert get_park_completion() == (None, None)
        assert get_park_completion() == (_COMPLETION_TOOL, _CONTEXT)
    finally:
        reset_park_completion(token)


def test_a_chained_dispatch_needs_a_run_that_can_park() -> None:
    # The chain is honored only where this run could actually park on the call. With no resume
    # continuation bound — a run that cannot park — a chain would address a key nothing will
    # ever park on, so the dispatch clears instead and the nested park is refused downstream.
    token = set_park_completion(_COMPLETION_TOOL, _CONTEXT)
    try:
        with nested_tool_dispatch(chain=True):
            assert get_park_completion() == (None, None)
    finally:
        reset_park_completion(token)


def test_a_chained_dispatch_binds_a_fresh_key_per_call_over_the_caller_binding() -> None:
    # One chained key addresses one CALL, so two dispatches are two keys: the terminals of two
    # nested runs can never converge on one park.
    resume = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
    token = set_park_completion(_COMPLETION_TOOL, _CONTEXT)
    try:
        keys = []
        for _ in range(2):
            with nested_tool_dispatch(chain=True):
                keys.append(_assert_chained_over(get_park_completion(), (_COMPLETION_TOOL, _CONTEXT)))
        assert keys[0] != keys[1]
        assert get_park_completion() == (_COMPLETION_TOOL, _CONTEXT)
    finally:
        reset_park_completion(token)
        reset_resume_continuation_tool(resume)


def test_a_chained_dispatch_carries_the_thread_index_field_up() -> None:
    # The platform's park-by-thread index reads ONE reserved field off whatever context is
    # bound. A chained dispatch replaces the caller's context, so it carries that field up —
    # a nested park under it stays cascade-cancellable with its thread.
    wrapped_context = {PARK_COMPLETION_THREAD_KEY: _THREAD, "route_name": "acme"}
    resume = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
    token = set_park_completion("deliver_tool_completion", wrapped_context)
    try:
        with nested_tool_dispatch(chain=True):
            _tool, context = get_park_completion()
            assert context is not None
            assert context[PARK_COMPLETION_THREAD_KEY] == _THREAD
            # Only that field. Everything else stays opaque, embedded rather than hoisted.
            assert "route_name" not in context
            assert context[CHAINED_PARK_CONTEXT_KEY]["context"]["route_name"] == "acme"
    finally:
        reset_park_completion(token)
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


def test_scoping_clears_the_binding_through_an_async_body() -> None:
    # The async half carries the SAME clearing property as the sync half — most client tools are
    # coroutine-bodied, so asserting only the sync path would leave the common case unpinned.
    seen: list[tuple[str | None, Any]] = []

    async def peek() -> str:
        """Peek at the binding."""
        seen.append(get_park_completion())
        return "peeked"

    scoped = scope_nested_dispatch(
        StructuredTool.from_function(func=None, coroutine=peek, name="peek", description="Peek at the binding.")
    )

    async def drive() -> Any:
        assert scoped.coroutine is not None
        return await scoped.coroutine()

    token = set_park_completion(_COMPLETION_TOOL, _CONTEXT)
    try:
        assert asyncio.run(drive()) == "peeked"
        assert get_park_completion() == (_COMPLETION_TOOL, _CONTEXT)
    finally:
        reset_park_completion(token)

    assert seen == [(None, None)]


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
