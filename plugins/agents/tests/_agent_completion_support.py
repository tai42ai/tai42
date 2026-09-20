"""Shared rig for the completion-continuation test modules: the scripted model and
sequential-ask stand-ins, the provider/registry fakes, the tools_agent and
deep-agent wiring/park helpers, and the expected-delivery payload builders.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    get_resume_continuation_tool,
    reset_park_completion,
    set_park_completion,
    suspended_interaction_marker,
)
from tai42_contract.template import TemplatedText

from tai42_agents import tools_agent as tools_mod
from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import capability as park_capability
from tai42_agents._internal.park import index as idx
from tai42_agents._internal.park import resume as park_resume
from tai42_agents.langchain_deep_agent import agent as agent_mod

_COMPLETION_TOOL = "conversation_deliver"


def _completion_context(thread_id: str) -> dict[str, Any]:
    """The opaque routing context a completion binder pairs with the tool — the delivery address
    the tool reads, keyed by ITS own parameter. Mirrors what the conversation door binds around
    an agent turn; the driver carries it verbatim and never interprets it."""
    return {"thread_id": thread_id}


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
        # the park retention to min(checkpoint, workspace), so its parks must carry an ask
        # deadline within that horizon; the non-durable ``tools_agent`` keeps a keep-forever
        # (None) retention and its parks pass with no deadline.
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            if get_resume_continuation_tool() is None:
                raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
            interaction_id = self._ids[self.calls]
            self.calls += 1
            return suspended_interaction_marker(interaction_id, self._expiry_at, get_resume_continuation_tool())

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _ask_call(call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "ask", "args": {}}])


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
    monkeypatch.setattr(park_capability, "llm_provider_settings", _ProviderSettings)


def _agent() -> tools_mod.ToolsAgent:
    agent = tai42_app.agents.get_agent("tools_agent")
    assert isinstance(agent, tools_mod.ToolsAgent)
    return agent


async def _park_via_astream(agent: tools_mod.ToolsAgent, thread_id: str) -> None:
    """Drive a fresh turn through the astream face with the completion tool bound, so the
    run parks with a completion delivery path recorded on its park entry."""
    token = set_park_completion(_COMPLETION_TOOL, _completion_context(thread_id))
    try:
        async for _event in agent.astream(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id=thread_id,
        ):
            pass
    finally:
        reset_park_completion(token)


def _expected_delivery(thread_id: str, interaction_ids: list[str], result: Any) -> dict[str, Any]:
    """The exact kwargs a clean-terminal completion handoff fires: the bound opaque context, the
    final answer, the stable completion id derived from (thread_id, super-step of the resolved
    interactions), and the succeeded terminal status."""
    completion_id = park_resume._completion_id(thread_id, idx.compute_superstep_id(interaction_ids))
    return {
        **_completion_context(thread_id),
        "result": result,
        "completion_id": completion_id,
        "status": PARK_COMPLETION_SUCCEEDED,
    }


def _expected_failed_delivery(thread_id: str, interaction_ids: list[str]) -> dict[str, Any]:
    """The exact kwargs the abandonment fire dispatches: the bound opaque context, a ``None``
    result (nothing was produced), the SAME stable completion id a clean terminal would use, and
    the FAILED terminal status."""
    completion_id = park_resume._completion_id(thread_id, idx.compute_superstep_id(interaction_ids))
    return {
        **_completion_context(thread_id),
        "result": None,
        "completion_id": completion_id,
        "status": PARK_COMPLETION_FAILED,
    }


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
    monkeypatch.setattr(park_capability, "llm_provider_settings", _ProviderSettings)


async def _park_via_astream_deep(agent: Any, thread_id: str) -> None:
    """Drive a fresh langchain_deep_agent turn through the astream face with the completion tool bound,
    so the run parks with a completion delivery path recorded on its park entry."""
    token = set_park_completion(_COMPLETION_TOOL, _completion_context(thread_id))
    try:
        async for _event in agent.astream(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id=thread_id,
        ):
            pass
    finally:
        reset_park_completion(token)
