"""Shared rig for the ``tools_agent`` park/resume test modules: the scripted model
and ask stand-ins, the provider/registry fakes, the build-wiring helper, and the
park-payload helpers the park behaviour tests drive on.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    SuspendedInteraction,
    get_chained_resume,
    get_resume_continuation_tool,
    suspended_interaction_marker,
)

from tai42_agents import tools_agent as tools_mod
from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import capability as park_capability


class ScriptedChatModel(BaseChatModel):
    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)
    # Every prompt the loop fed the model, so a test can assert what the MODEL saw of a
    # tool outcome (a refusal has to reach the model to be answerable in the same turn).
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        message = self._responses[self._index]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


class _AskStandIn:
    """A faithful ``ask(mode="async")`` stand-in: it parks (returns the reserved
    marker) ONLY when a resume continuation is bound, and otherwise RAISES exactly as the
    platform helper does with no driver bound — so a face that binds no resume path refuses
    loudly. Counts calls so a resume that re-ran the tool (a double-park) is caught."""

    def __init__(self, interaction_id: str, expiry_at: Any = None, *, caller: bool = False) -> None:
        self.calls = 0
        self._interaction_id = interaction_id
        self._expiry_at = expiry_at
        # A ``to="caller"`` ask stamps its own id into the marker's caller subset (the platform's
        # ``ask`` does this off ``to``); a user ask leaves it empty.
        self._caller = caller

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            self.calls += 1
            if get_resume_continuation_tool() is None:
                raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
            return suspended_interaction_marker(
                self._interaction_id,
                self._expiry_at,
                get_resume_continuation_tool(),
                caller_interaction_ids=[self._interaction_id] if self._caller else [],
            )

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _ask_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "ask", "args": {}}])


class _NestedDriverStandIn:
    """A base tool that runs a NESTED DRIVER which async-parks — a nested-driver preset, another
    agent run — the shape the agent loop cannot resume through its own continuation.

    The nested driver binds its own resume continuation for the ask, so the platform stamps
    THAT continuation onto the parked interaction and drives the resume there; the receipt
    coming back names it as the park's ``resume_owner``. Counts calls so a test can assert the
    nested run happened (its park is real and untouched), and records the chained key it saw
    bound around the dispatch — which is the address its driver would later fire its terminal
    at, and empty when the caller chained nothing."""

    def __init__(self, interaction_id: str, resume_owner: str | None, expiry_at: datetime | None = None) -> None:
        self.calls = 0
        self.chained_keys: list[str] = []
        self._interaction_id = interaction_id
        self._resume_owner = resume_owner
        self._expiry_at = expiry_at

    def run(self, **_kwargs: Any) -> SuspendedInteraction:
        self.calls += 1
        routing = get_chained_resume()
        if routing is not None:
            self.chained_keys.append(routing.chain_key)
        return SuspendedInteraction(
            interaction_id=self._interaction_id, resume_owner=self._resume_owner, expiry_at=self._expiry_at
        )

    def base_tool(self) -> StructuredTool:
        def nested_driver(marker: str = "") -> str:
            """Run the nested driver."""
            return marker

        return StructuredTool.from_function(nested_driver, name="nested_driver", description="Run the nested driver.")


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
    monkeypatch.setattr(park_capability, "llm_provider_settings", _ProviderSettings)


def _agent() -> tools_mod.ToolsAgent:
    agent = tai42_app.agents.get_agent("tools_agent")
    assert isinstance(agent, tools_mod.ToolsAgent)
    return agent


def _preset_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "driver_preset", "args": {}}])


def _tool_messages(prompt: list[Any]) -> list[Any]:
    return [message for message in prompt if isinstance(message, ToolMessage)]


class _RawMarkerTool:
    """A tool whose RESULT is a park marker this run never minted — the wire form, as a
    non-park-capable middle agent hands it on, as an older wire form carries it, or as a
    model could shape it. No sentinel object ever crosses a tool face here, so only the
    CLAIM point can catch it."""

    def __init__(self, marker: dict[str, Any]) -> None:
        self.calls = 0
        self._marker = marker

    def tool(self) -> StructuredTool:
        def relay() -> dict[str, Any]:
            self.calls += 1
            return self._marker

        return StructuredTool.from_function(relay, name="relay", description="Relay a nested result.")


def _relay_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "relay", "args": {}}])


def _park() -> park_capability.ParkIdentity:
    return park_capability.ParkIdentity(agent_name="a", thread_id="t", rebuild_kwargs={}, bind=True)
