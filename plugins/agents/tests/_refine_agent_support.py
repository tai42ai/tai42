"""Shared rig for the ``refine_agent`` test modules: the fake create-agent recorder
and agent, provider/registry/logging fakes, the loop patchers, the final-pass
and structured-stream scripts, and the recording chat model.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.agent import (
    Agent,
    StreamEvent,
)

import tai42_agents.refine_agent.agent as agent_mod

AGENT_NAME = "refine_agent"


class FakeAgent:
    """A stand-in compiled LangGraph agent.

    ``ainvoke`` pops the next canned content off ``invoke_contents`` and wraps it
    in a one-message state (what ``build_user_output`` reads). ``astream`` replays
    a fixed list of ``(mode, chunk)`` items for the final-pass projection.
    """

    def __init__(
        self, invoke_contents: list[str], stream_items: list[Any] | None = None, history: list[Any] | None = None
    ) -> None:
        self._invoke_contents = list(invoke_contents)
        self._stream_items = list(stream_items or [])
        self._history = list(history or [])
        self.ainvoke_inputs: list[Any] = []
        self.astream_inputs: list[Any] = []
        self.get_state_calls = 0

    async def ainvoke(self, agent_input: Any, config: Any) -> dict[str, Any]:
        self.ainvoke_inputs.append(agent_input)
        content = self._invoke_contents.pop(0)
        return {"messages": [AIMessage(content=content)]}

    async def astream(self, agent_input: Any, config: Any, stream_mode: Any = None):
        self.astream_inputs.append(agent_input)
        for item in self._stream_items:
            yield item

    async def aget_state(self, config: Any) -> Any:
        # The structured final-pass reads the loop thread's negotiation history from
        # the checkpoint here; a fake snapshot exposes ``.values["messages"]``.
        self.get_state_calls += 1
        return SimpleNamespace(values={"messages": list(self._history)})


class CreateAgentRecorder:
    """A ``create_agent`` replacement: hands out queued fakes and records the
    tools, per-run ``system_prompt``, and ``response_format`` each was compiled
    with (the latter is ``None`` on the text-loop evaluator/critic and the
    schema strategy only on the structured final pass)."""

    def __init__(self, agents: list[FakeAgent]) -> None:
        self._queue = list(agents)
        self.tools_per_call: list[list[Any]] = []
        self.system_prompts: list[Any] = []
        self.middlewares_per_call: list[list[Any]] = []
        self.response_formats: list[Any] = []

    def __call__(
        self,
        llm: Any,
        *,
        tools: Any,
        checkpointer: Any,
        middleware: Any,
        debug: Any,
        response_format: Any = None,
        system_prompt: Any = None,
    ) -> FakeAgent:
        self.tools_per_call.append(list(tools))
        self.system_prompts.append(system_prompt)
        self.middlewares_per_call.append(list(middleware))
        self.response_formats.append(response_format)
        return self._queue.pop(0)


class _ProviderSettings:
    llm = "fake-llm"
    checkpoint = "fake-ckpt"
    checkpoint_conn_string = None


class _LlmSettings:
    def with_fallbacks(self, kwargs: dict[str, Any] | None) -> dict[str, Any]:
        return {}


class _CheckpointRegistry:
    async def get_checkpointer(self, provider: str, conn_string: Any) -> None:
        return None


class _LoggingSettings:
    def is_enabled_for(self, level: str) -> bool:
        return False


def _patch_loop(monkeypatch: pytest.MonkeyPatch, agents: list[FakeAgent]) -> CreateAgentRecorder:
    """Monkeypatch every non-loop seam so ``_run_refine_loop`` runs offline
    against the supplied fake evaluator/critic. Returns the create-agent recorder
    (evaluator is compiled first, critic second)."""
    recorder = CreateAgentRecorder(agents)
    monkeypatch.setattr(agent_mod, "create_agent", recorder)

    async def _get_llm_async(provider: str, **kwargs: Any) -> str:
        return "llm"

    monkeypatch.setattr(agent_mod, "get_llm_async", _get_llm_async)
    monkeypatch.setattr(agent_mod, "checkpoint_registry", lambda: _CheckpointRegistry())
    monkeypatch.setattr(agent_mod, "context_overflow_middlewares", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_mod, "logging_settings", lambda: _LoggingSettings())
    monkeypatch.setattr(agent_mod, "llm_provider_settings", lambda: _ProviderSettings())
    monkeypatch.setattr(agent_mod, "llm_settings", lambda: _LlmSettings())

    def _init_config(config: dict[str, Any] | None) -> dict[str, Any]:
        return config or {"configurable": {"thread_id": "t"}}

    monkeypatch.setattr(agent_mod, "init_langgraph_config", _init_config)
    return recorder


def _final_pass_script() -> list[Any]:
    """A final-evaluator ``astream`` script exercising the whole event taxonomy:
    reasoning, a tool call + result, token deltas, and a structured response."""
    reasoning_and_call = AIMessage(
        content=[{"type": "thinking", "thinking": "weighing the options"}],
        tool_calls=[{"id": "c1", "name": "lookup", "args": {"q": 1}}],
    )
    usage_message = AIMessage(
        content="",
        usage_metadata={"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
        response_metadata={"model_name": "fake-model"},
    )
    tool_result = ToolMessage(content="tool said hi", name="lookup", tool_call_id="c1")
    return [
        ("updates", {"model": {"messages": [reasoning_and_call]}}),
        ("updates", {"tools": {"messages": [tool_result]}}),
        ("updates", {"model": {"messages": [usage_message]}}),
        ("messages", (AIMessageChunk(content="Final "), {})),
        ("messages", (AIMessageChunk(content="answer"), {})),
        ("updates", {"model": {"messages": [], "structured_response": {"ok": True}}}),
    ]


def _collect(agent: Agent, **kwargs: Any) -> list[StreamEvent]:
    async def go() -> list[StreamEvent]:
        return [event async for event in agent.astream(**kwargs)]

    return asyncio.run(go())


def _a_tool() -> StructuredTool:
    def _f(x: int) -> int:
        return x

    return StructuredTool.from_function(func=_f, name="t1", description="a tool")


def _structured_stream_items(payload: dict[str, Any]) -> list[Any]:
    """A structured final-pass ``astream`` script: one updates chunk carrying the
    forced structured response the projection turns into a ``StructuredFinal``."""
    return [("updates", {"model": {"messages": [], "structured_response": payload}})]


class _RecordingChatModel(BaseChatModel):
    """Returns a fresh ``AIMessage`` carrying a fixed content on every call (a new id
    each time, so the checkpoint reducer appends rather than dedups) and records the
    exact message list each model call received; ``bind_tools`` is a no-op."""

    _content: str = PrivateAttr()
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, content: str, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._content = content

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._content))])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _mark_count(messages: list[BaseMessage]) -> int:
    """Number of messages carrying a ``cache_control`` block (a cache breakpoint)."""
    return sum(
        isinstance(m.content, list) and any(isinstance(b, dict) and "cache_control" in b for b in m.content)
        for m in messages
    )


def _patch_loop_real(
    monkeypatch: pytest.MonkeyPatch,
    evaluator_model: BaseChatModel,
    critic_model: BaseChatModel,
    saver: InMemorySaver,
) -> list[Any]:
    """Patch every non-loop seam so ``_run_refine_loop`` compiles REAL ``create_agent``
    graphs (real middleware wiring) over the two provider-keyed models and one shared
    checkpointer. Returns a list the spied ``create_agent`` appends each compiled agent
    to (evaluator first, critic second, per run)."""
    created: list[Any] = []
    real_create_agent = agent_mod.create_agent

    def spy_create_agent(*args: Any, **kwargs: Any) -> Any:
        agent = real_create_agent(*args, **kwargs)
        created.append(agent)
        return agent

    monkeypatch.setattr(agent_mod, "create_agent", spy_create_agent)

    async def _get_llm_async(provider: str, **kwargs: Any) -> BaseChatModel:
        return evaluator_model if provider == "eval" else critic_model

    monkeypatch.setattr(agent_mod, "get_llm_async", _get_llm_async)

    async def _get_checkpointer(provider: str, conn_string: Any) -> InMemorySaver:
        return saver

    monkeypatch.setattr(agent_mod, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=_get_checkpointer))
    monkeypatch.setattr(agent_mod, "context_overflow_middlewares", AsyncMock(return_value=[]))
    monkeypatch.setattr(agent_mod, "logging_settings", lambda: _LoggingSettings())
    monkeypatch.setattr(agent_mod, "llm_provider_settings", lambda: _ProviderSettings())
    monkeypatch.setattr(agent_mod, "llm_settings", lambda: _LlmSettings())
    # Keep the caller's thread_id so the two runs land on the same checkpointed thread.
    monkeypatch.setattr(
        agent_mod, "init_langgraph_config", lambda config: config or {"configurable": {"thread_id": "t"}}
    )
    return created
