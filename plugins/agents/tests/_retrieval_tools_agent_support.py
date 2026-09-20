"""Shared rig for the ``retrieval_tools_agent`` test modules: the tool factory,
stub/recording stores, scripted and structured chat models, the graph builder,
cache-mark helpers, and the build-seam patcher.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr

from tai42_agents.retrieval_tools_agent import agent as ragent
from tai42_agents.retrieval_tools_agent.agent import (
    _embedding_dims_cache,
)
from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph

AGENT_NAME = "retrieval_tools_agent"


def _tool(name: str, description: str = "d") -> StructuredTool:
    async def _run(**_: Any) -> str:
        return "ok"

    return StructuredTool.from_function(func=None, coroutine=_run, name=name, description=description)


class StubStore(InMemoryStore):
    """A real ``BaseStore`` (so langgraph injects it) whose semantic ``asearch``
    returns a fixed key list instead of running a vector index."""

    def __init__(self, keys: list[str]) -> None:
        super().__init__()
        self._keys = keys

    async def asearch(self, namespace_prefix: Any, /, **kwargs: Any) -> Any:  # type: ignore[override]
        return [SimpleNamespace(key=key) for key in self._keys]


class RecordingStore(InMemoryStore):
    """A real ``BaseStore`` (so ``InjectedStore`` validation accepts it) that
    records the namespace passed to each call so the per-tool-set namespace can
    be asserted, and returns canned search results."""

    def __init__(self) -> None:
        super().__init__()
        self.puts: list[tuple[Any, str, dict[str, Any]]] = []
        self.deletes: list[tuple[Any, str]] = []
        self.searches: list[tuple[Any, str | None, int]] = []

    async def aget(self, namespace: Any, key: str, **kwargs: Any) -> Any:
        return None

    async def aput(self, namespace: Any, key: str, value: dict[str, Any], *args: Any, **kwargs: Any) -> None:
        self.puts.append((namespace, key, value))

    async def adelete(self, namespace: Any, key: str) -> None:
        self.deletes.append((namespace, key))

    async def asearch(
        self, namespace_prefix: Any, /, *, query: str | None = None, limit: int = 10, **kwargs: Any
    ) -> Any:
        self.searches.append((namespace_prefix, query, limit))
        return [SimpleNamespace(key="id1"), SimpleNamespace(key="id2")]


class ScriptedChatModel(BaseChatModel):
    """Returns a fixed list of messages in order and records the exact message
    list each model call received; ``bind_tools`` is a no-op so the agent node
    can bind the retrieve/selected tools and still be scripted."""

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        message = self._responses[self._index]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


_RETRIEVAL_SCHEMA = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}


class _StructuredLLM:
    """A stand-in chat model whose ``with_structured_output`` binds a schema and
    returns a runner whose ``ainvoke`` yields a fixed structured payload."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload
        self.captured: dict[str, Any] = {}

    def with_structured_output(self, schema: Any, include_raw: bool = True) -> Any:
        self.captured["schema"] = schema
        self.captured["include_raw"] = include_raw
        outer = self

        class _Runner:
            async def ainvoke(self, messages: Any) -> Any:
                outer.captured["messages"] = messages
                return outer._payload

        return _Runner()


class _BoomStructuredLLM:
    """A structured llm whose finalization ``ainvoke`` raises, standing in for an
    unparseable structured-output response that must propagate loudly."""

    def with_structured_output(self, schema: Any, include_raw: bool = True) -> Any:
        class _Runner:
            async def ainvoke(self, messages: Any) -> Any:
                raise ValueError("model returned unparseable structured output")

        return _Runner()


def _graph(**kwargs: Any) -> RetrievalToolsGraph:
    return RetrievalToolsGraph(tools=[], llm=MagicMock(), **kwargs)


async def _collect(agen: AsyncIterator[Any]) -> list[Any]:
    return [event async for event in agen]


def _mark_count(messages: list[BaseMessage]) -> int:
    """Number of messages carrying a ``cache_control`` block (a cache breakpoint)."""
    return sum(
        isinstance(m.content, list) and any(isinstance(b, dict) and "cache_control" in b for b in m.content)
        for m in messages
    )


def _marked_user(text: str) -> dict[str, Any]:
    block = {"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}
    return {"messages": [{"role": "user", "content": [block]}]}


def _patch_build_seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Monkeypatch every provider seam ``_build`` reaches, and return the
    captured wiring so a test can assert defaults were resolved and threaded."""
    captured: dict[str, Any] = {}

    provider_settings = SimpleNamespace(
        llm="def_llm",
        embedding="def_embedding",
        checkpoint="def_checkpoint",
        store="def_store",
        store_conn_string="store-conn",
        checkpoint_conn_string="checkpoint-conn",
    )
    monkeypatch.setattr(ragent, "llm_provider_settings", lambda: provider_settings)
    monkeypatch.setattr(ragent, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))
    monkeypatch.setattr(
        ragent, "embedding_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs))
    )

    async def fake_resolve_tools(app_tools: Any, names: list[str], tools: list[Any], presets: list[Any]) -> list[Any]:
        captured["resolve"] = (names, tools, presets)
        return ["resolved-tool"]

    monkeypatch.setattr(ragent, "resolve_tools", fake_resolve_tools)

    async def fake_get_llm(*, provider: str, **kwargs: Any) -> Any:
        captured["llm_provider"] = provider
        captured["llm_kwargs"] = kwargs
        return "llm-obj"

    monkeypatch.setattr(ragent, "get_llm_async", fake_get_llm)

    embedding = MagicMock()
    embedding.aembed_query = AsyncMock(return_value=[0.0] * 6)

    async def fake_get_embedding(*, provider: str, **kwargs: Any) -> Any:
        captured["embedding_provider"] = provider
        captured["embedding_kwargs"] = kwargs
        return embedding

    monkeypatch.setattr(ragent, "get_embedding_async", fake_get_embedding)

    async def fake_get_store(*, provider: str, conn_string: str, **kwargs: Any) -> Any:
        captured["store"] = (provider, conn_string, kwargs)
        return "store-obj"

    monkeypatch.setattr(ragent, "store_registry", lambda: SimpleNamespace(get_store=fake_get_store))

    async def fake_get_checkpointer(*, provider: str, conn_string: str) -> Any:
        captured["checkpoint"] = (provider, conn_string)
        return "checkpointer-obj"

    monkeypatch.setattr(ragent, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))

    class _FakeCompiled:
        # A faithful compiled-graph stand-in: the turn-start repair reads ``aget_state``,
        # and an empty message log means a non-poisoned thread (repair is a no-op).
        async def aget_state(self, config: Any) -> SimpleNamespace:
            return SimpleNamespace(values={}, interrupts=())

    class FakeGraph:
        def __init__(self, **kwargs: Any) -> None:
            captured["graph_kwargs"] = kwargs

        async def abuild(self) -> _FakeCompiled:
            compiled = _FakeCompiled()
            captured["compiled"] = compiled
            return compiled

    monkeypatch.setattr(ragent, "RetrievalToolsGraph", FakeGraph)
    monkeypatch.setattr(ragent, "init_langgraph_config", lambda config: {"configurable": {"thread_id": "t"}})
    _embedding_dims_cache.clear()
    return captured
