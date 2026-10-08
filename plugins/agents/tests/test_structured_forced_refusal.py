"""The headline: a model that refuses forced tool choice still yields structured output.

A scripted model declares native structured output and refuses a forced ``tool_choice``
(its ``bind_tools`` raises the vendor's 400 text when one is bound), answering a JSON-text
turn when the native ``response_format`` kwargs are bound instead. Driven through the
tools-agent invoke and stream faces (and the deep-agent factory build), the native plan
binds no forced tool choice, so the run produces the validated structured object — where
the old forced-tool path raised the provider's refusal (the control assertion proves the
refusal is real).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.agent.events import MessageDelta, MessageFinal, StructuredFinal, ToolCallStep, ToolResultStep

from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal.stream_events import aproject_agent_events
from tai42_agents._internal.structured import structured_output_stack

from ._graph_support import invoke_tools_agent, stream_tools_agent_events
from .conftest import fake_run_trace

_SCHEMA = {
    "title": "TurnIntake",
    "type": "object",
    "properties": {"value": {"type": ["integer", "null"]}},
    "required": ["value"],
}

_FORCED_CHOICES = ("any", "tool", "required")


class _NativeRefusingModel(BaseChatModel):
    """Declares native structured output, refuses a forced ``tool_choice`` and answers JSON text.

    ``bind_tools`` raises the vendor's refusal when a forced ``tool_choice`` is bound (the old
    forced-tool path), records the bound kwargs otherwise, and the model answers scripted JSON.
    """

    _texts: list[str] = PrivateAttr(default_factory=list)
    calls: int = 0

    def __init__(self, texts: Sequence[str], **kwargs: Any) -> None:
        super().__init__(profile={"structured_output": True, "tool_choice": False}, **kwargs)
        self._texts = list(texts)

    @property
    def _llm_type(self) -> str:
        return "native-refusing"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        choice = kwargs.get("tool_choice")
        if choice in _FORCED_CHOICES or (isinstance(choice, dict) and choice.get("type") in _FORCED_CHOICES):
            raise ValueError('tool_choice: type "tool" and "any" are not supported for this model.')
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        index = min(self.calls, len(self._texts) - 1)
        self.calls += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self._texts[index]))])


def _patch_seams(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel) -> None:
    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=3),
    )
    monkeypatch.setattr(
        bta, "llm_provider_settings", lambda: SimpleNamespace(llm="openai", checkpoint="c", checkpoint_conn_string="s")
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))

    async def _get_llm(*, provider: str, **kwargs: Any) -> BaseChatModel:
        return model

    monkeypatch.setattr(bta, "get_llm_async", _get_llm)
    saver = InMemorySaver()

    async def _get_checkpointer(*, provider: str, conn_string: str) -> Any:
        return saver

    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=_get_checkpointer))

    async def _no_overflow(*, system_prompt: Any) -> list[Any]:
        return []

    monkeypatch.setattr(bta, "context_overflow_middlewares", _no_overflow)
    monkeypatch.setattr(bta, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: False))
    monkeypatch.setattr(
        bta,
        "init_langgraph_config",
        lambda config: fake_run_trace({"configurable": {"thread_id": "t"}, "recursion_limit": 50}),
    )


def test_forced_tool_choice_is_refused_by_the_model() -> None:
    # Control: the model genuinely refuses a forced tool choice — the old forced-tool path.
    model = _NativeRefusingModel(['{"value": 7}'])
    with pytest.raises(ValueError, match="not supported for this model"):
        model.bind_tools([], tool_choice="any")


def test_invoke_face_produces_the_structured_object_under_the_native_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    model = _NativeRefusingModel(['{"value": 7}'])
    _patch_seams(monkeypatch, model)

    result = asyncio.run(invoke_tools_agent(system_message="", user_message=["go"], tools=[], response_format=_SCHEMA))
    # No forced tool choice was sent, so the refusing model answered and the native plan
    # landed the validated structured object.
    assert result.structured == {"value": 7}
    assert result.outcome is None


def test_stream_face_yields_one_structured_final_and_no_tool_or_message_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = _NativeRefusingModel(['{"value": 7}'])
    _patch_seams(monkeypatch, model)

    async def _collect() -> list[Any]:
        return [
            event
            async for event in stream_tools_agent_events(
                system_message="", user_message=["go"], tools=[], response_format=_SCHEMA
            )
        ]

    events = asyncio.run(_collect())
    assert any(isinstance(event, StructuredFinal) and event.data == {"value": 7} for event in events)
    assert not any(isinstance(event, ToolCallStep | ToolResultStep) for event in events)
    # Under the native plan the JSON payload is NOT streamed as message deltas / a final.
    assert not any(isinstance(event, MessageDelta | MessageFinal) for event in events)


def test_deep_agent_build_produces_the_structured_object_under_the_native_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from langgraph.store.memory import InMemoryStore

    from tai42_agents.langchain_deep_agent.factory import build_langchain_deep_agent

    monkeypatch.setattr(
        "tai42_agents._internal.structured.agents_limits_settings",
        lambda: SimpleNamespace(structured_output_reprompt_cap=3),
    )
    model = _NativeRefusingModel(['{"value": 7}'])
    graph = asyncio.run(
        build_langchain_deep_agent(
            llm=model,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            provider="openai",
            tools=[],
            response_format=_SCHEMA,
        )
    )
    strategy, _rail = structured_output_stack(model, "openai", _SCHEMA)

    async def _collect() -> list[Any]:
        return [
            event
            async for event in aproject_agent_events(
                graph,
                {"messages": [HumanMessage(content="go")]},
                {"configurable": {"thread_id": "t"}, "recursion_limit": 50},
                response_format=_SCHEMA,
                structured_strategy=strategy,
            )
        ]

    events = asyncio.run(_collect())
    assert any(isinstance(event, StructuredFinal) and event.data == {"value": 7} for event in events)


def test_old_forced_tool_strategy_would_raise_the_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    # Red-path control: the pre-design forced ToolStrategy path binds tool_choice="any",
    # which this model refuses — so the structured run raised the provider's 400. The native
    # plan above avoids it entirely.
    model = _NativeRefusingModel(['{"value": 7}'])
    graph = create_agent(model, tools=[], checkpointer=InMemorySaver(), response_format=ToolStrategy(_SCHEMA))
    with pytest.raises(ValueError, match="not supported for this model"):
        asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}, {"configurable": {"thread_id": "t"}}))
