"""``run`` and ``astream`` hand back the same final text: the reply text of the last message.

Driven over a REAL ``create_agent`` graph with a scripted model whose final answer opens with a
thinking block and splits its reply over two text blocks (the shape a reasoning model with
citations answers in).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.agent import MessageFinal

from tai42_agents._internal import base_tool_agent as bta

from ._graph_support import invoke_tools_agent, stream_tools_agent_events
from .conftest import fake_run_trace

_FINAL = AIMessage(
    content=[
        {"type": "thinking", "thinking": "weigh it", "signature": "sig"},
        {"type": "text", "text": "Hello, "},
        {"type": "text", "text": "world"},
    ]
)


class _ScriptedChatModel(BaseChatModel):
    """Answers every call with the next scripted message; ``bind_tools`` keeps the script."""

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        message = self._responses[self._index]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _seams(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel) -> None:
    monkeypatch.setattr(
        bta,
        "llm_provider_settings",
        lambda: SimpleNamespace(llm="anthropic", checkpoint="c", checkpoint_conn_string=None),
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))

    async def fake_get_llm(*, provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(bta, "get_llm_async", fake_get_llm)
    saver = InMemorySaver()

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))
    monkeypatch.setattr(bta, "context_overflow_middlewares", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        bta,
        "init_langgraph_config",
        lambda config: fake_run_trace({"configurable": {"thread_id": config["configurable"]["thread_id"]}}),
    )


def test_run_and_astream_return_the_same_final_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _seams(monkeypatch, _ScriptedChatModel([_FINAL]))
    result = asyncio.run(invoke_tools_agent("sys", ["hi"], [], config={"configurable": {"thread_id": "t-run"}}))

    _seams(monkeypatch, _ScriptedChatModel([_FINAL]))

    async def _collect() -> list[Any]:
        return [
            event
            async for event in stream_tools_agent_events(
                "sys", ["hi"], [], config={"configurable": {"thread_id": "t-stream"}}
            )
        ]

    finals = [event for event in asyncio.run(_collect()) if isinstance(event, MessageFinal)]

    assert result.output == "Hello, world"
    assert [final.text for final in finals] == ["Hello, world"]
