"""The system prompt is per-run model configuration, never checkpointed state.

Driven over a REAL ``create_agent`` graph (a scripted fake chat model + an
in-memory checkpointer, no LLM or network) through the tools-agent faces:

* the system prompt reaches the model on every turn — plain string and
  ``cache_control`` content-block form alike — while the thread's checkpointed
  messages stay system-free, so a reused ``thread_id`` never accumulates
  system messages;
* a thread whose stored history already carries a ``SystemMessage`` runs
  cleanly: the purge middleware removes it before the model call, so the model
  sees exactly the one per-run system prompt;
* a raw-dict ``response_format`` runs through the tool-calling strategy end to
  end and still lands the validated object on ``state["structured_response"]``.

Async code is driven with ``asyncio.run`` (the suite does not use pytest-asyncio).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.structured_output import ProviderStrategy, ToolStrategy
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import START
from pydantic import BaseModel, PrivateAttr
from tai42_contract.agent.events import StructuredFinal, ToolCallStep, ToolResultStep

import tai42_agents._internal.stream_events as stream_events
from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal.stream_events import _structured_tool_names, astream_tools_agent_events


class RecordingChatModel(BaseChatModel):
    """Returns a fixed list of messages in order (the last is repeated if the run
    asks for more) and records the exact message list each model call received;
    ``bind_tools`` is a no-op so the agent can bind its tools."""

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        message = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _seams(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
    """Patch every provider seam ``_build_agent_and_input`` reaches so a REAL
    ``create_agent`` graph is compiled over ``model`` + ``saver`` — the middleware
    wiring, system-prompt build, config init, and message build stay real."""
    monkeypatch.setattr(
        bta,
        "llm_provider_settings",
        lambda: SimpleNamespace(llm="p", checkpoint="c", checkpoint_conn_string=None),
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))

    async def fake_get_llm(*, provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(bta, "get_llm_async", fake_get_llm)

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))
    # No context-overflow strategies in the test: keep the middleware list to the
    # system-purge / leading-user / tool-error middleware the factory attaches.
    monkeypatch.setattr(bta, "context_overflow_middlewares", lambda system_prompt=None: [])
    # The real config init wires the recording monitoring stub's non-callable
    # callback sentinels, which the live graph would try to invoke; keep the
    # caller's thread_id and nothing else.
    monkeypatch.setattr(
        bta,
        "init_langgraph_config",
        lambda config: {"configurable": {"thread_id": config["configurable"]["thread_id"]}},
    )


def _config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}}


async def _state_messages(thread_id: str) -> list[BaseMessage]:
    """Read a thread's checkpointed messages back through a freshly built agent over
    the same checkpointer."""
    agent, _, config = await bta._build_agent_and_input("sys", ["x"], [], config=_config(thread_id))
    snapshot = await agent.aget_state(config)
    return snapshot.values.get("messages", []) if snapshot.values else []


class TestSystemPromptIsPerRun:
    def test_system_prompt_reaches_model_but_never_state(self, monkeypatch: pytest.MonkeyPatch) -> None:
        model = RecordingChatModel([AIMessage(content="first"), AIMessage(content="second")])
        _seams(monkeypatch, model, InMemorySaver())

        result = asyncio.run(bta.ainvoke_tools_agent("be brief", ["hi"], [], config=_config("t-per-run")))
        assert result.output == "first"

        # The model call carried the system prompt first, ahead of the user turn.
        assert isinstance(model._seen[0][0], SystemMessage)
        assert model._seen[0][0].content == "be brief"
        # The checkpointed thread stays system-free: user + assistant only.
        messages = asyncio.run(_state_messages("t-per-run"))
        assert [type(m) for m in messages] == [HumanMessage, AIMessage]

    def test_reused_thread_gets_the_system_prompt_on_every_run_without_accumulating(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = RecordingChatModel([AIMessage(content="first"), AIMessage(content="second")])
        saver = InMemorySaver()
        _seams(monkeypatch, model, saver)

        asyncio.run(bta.ainvoke_tools_agent("be brief", ["hi"], [], config=_config("t-reuse")))
        result = asyncio.run(bta.ainvoke_tools_agent("be brief", ["again"], [], config=_config("t-reuse")))
        assert result.output == "second"

        # Both model calls led with exactly one system message — the per-run prompt.
        for seen in model._seen:
            assert isinstance(seen[0], SystemMessage)
            assert sum(isinstance(m, SystemMessage) for m in seen) == 1
        # The reused thread accumulated only the conversation, never a system message.
        messages = asyncio.run(_state_messages("t-reuse"))
        assert [type(m) for m in messages] == [HumanMessage, AIMessage, HumanMessage, AIMessage]

    def test_cache_control_block_form_reaches_model_and_stays_out_of_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = RecordingChatModel([AIMessage(content="done")])
        _seams(monkeypatch, model, InMemorySaver())

        asyncio.run(
            bta.ainvoke_tools_agent(
                "be brief",
                ["hi"],
                [],
                config=_config("t-cache"),
                system_content_kwargs={"cache_control": {"type": "ephemeral"}},
            )
        )

        # The outgoing request kept the structured content-block form.
        system = model._seen[0][0]
        assert isinstance(system, SystemMessage)
        assert system.content == [{"type": "text", "text": "be brief", "cache_control": {"type": "ephemeral"}}]
        # And the block-form prompt did not leak into the checkpointed thread either.
        messages = asyncio.run(_state_messages("t-cache"))
        assert not any(isinstance(m, SystemMessage) for m in messages)


class TestStoredSystemMessageIsPurged:
    def test_thread_with_stored_system_message_runs_cleanly_and_is_purged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = RecordingChatModel([AIMessage(content="ok")])
        saver = InMemorySaver()
        _seams(monkeypatch, model, saver)

        async def seed() -> None:
            # Seed a thread whose stored history carries a system message ahead of
            # the conversation, written straight into the checkpoint.
            agent, _, config = await bta._build_agent_and_input("sys", ["x"], [], config=_config("t-stale"))
            await agent.aupdate_state(
                config,
                {
                    "messages": [
                        SystemMessage(content="stored rules", id="stale-system"),
                        HumanMessage(content="earlier turn", id="h1"),
                        AIMessage(content="earlier answer", id="a1"),
                    ]
                },
                as_node=START,
            )

        asyncio.run(seed())
        result = asyncio.run(bta.ainvoke_tools_agent("be brief", ["hi"], [], config=_config("t-stale")))
        assert result.output == "ok"

        # The model saw exactly one system message: the per-run prompt, first —
        # never the stored one alongside it.
        seen = model._seen[0]
        systems = [m for m in seen if isinstance(m, SystemMessage)]
        assert len(systems) == 1
        assert seen[0] is systems[0]
        assert systems[0].content == "be brief"
        # The purge is persistent: the stored system message is gone from state.
        messages = asyncio.run(_state_messages("t-stale"))
        assert not any(isinstance(m, SystemMessage) for m in messages)
        assert [m.content for m in messages[:2]] == ["earlier turn", "earlier answer"]


class _Cat(BaseModel):
    meow: int


class _Dog(BaseModel):
    woof: int


class TestStructuredToolNamesDerivation:
    """``_structured_tool_names`` derives the suppressed synthetic-tool name set by
    routing ``response_format`` through the same ``as_tool_strategy`` wrap the graph
    binds, so the names match langchain for every schema shape — including the
    union/oneOf shapes that fan out into one tool per variant."""

    def test_pydantic_class_binds_its_name(self) -> None:
        assert _structured_tool_names(_Cat) == frozenset({"_Cat"})

    def test_union_of_models_suppresses_every_variant_name(self) -> None:
        # A raw Python union binds one synthetic tool PER variant, so BOTH variant
        # names must be suppressed (a single top-level name would leak the other).
        assert _structured_tool_names(_Cat | _Dog) == frozenset({"_Cat", "_Dog"})

    def test_titled_oneof_dict_suppresses_per_variant_titles_not_the_top(self) -> None:
        # oneOf fans out the same way: the per-variant titles are the bound tool
        # names, never the container's top-level title.
        schema = {
            "title": "Top",
            "oneOf": [
                {"title": "A", "type": "object"},
                {"title": "B", "type": "object"},
            ],
        }
        assert _structured_tool_names(schema) == frozenset({"A", "B"})

    def test_explicit_multi_spec_tool_strategy_exposes_all_spec_names(self) -> None:
        assert _structured_tool_names(ToolStrategy(_Cat | _Dog)) == frozenset({"_Cat", "_Dog"})

    def test_provider_strategy_and_none_suppress_nothing(self) -> None:
        # Provider-native routing (ProviderStrategy) and no requested format bind no
        # synthetic tool, so nothing is suppressed.
        assert _structured_tool_names(ProviderStrategy(_Cat)) == frozenset()
        assert _structured_tool_names(None) == frozenset()


class TestToolStrategyEndToEnd:
    def test_dict_response_format_produces_structured_response_via_tool_calling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
        model = RecordingChatModel(
            [AIMessage(content="", tool_calls=[{"id": "call_1", "name": "Answer", "args": {"value": 7}}])]
        )
        _seams(monkeypatch, model, InMemorySaver())

        result = asyncio.run(
            bta.ainvoke_tools_agent("sys", ["hi"], [], config=_config("t-structured"), response_format=schema)
        )

        # The tool-calling strategy landed the validated object on the same
        # structured_response channel the platform extraction reads.
        assert result.structured == {"value": 7}

    def test_stream_projection_suppresses_the_synthetic_structured_tool_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        schema = {"title": "Answer", "type": "object", "properties": {"value": {"type": "integer"}}}
        model = RecordingChatModel(
            [AIMessage(content="", tool_calls=[{"id": "call_1", "name": "Answer", "args": {"value": 7}}])]
        )
        _seams(monkeypatch, model, InMemorySaver())

        async def collect() -> list[Any]:
            return [
                event
                async for event in astream_tools_agent_events(
                    "sys", ["hi"], [], config=_config("t-stream-structured"), response_format=schema
                )
            ]

        events = asyncio.run(collect())

        # The synthetic structured-output tool call is internal routing mechanics:
        # neither its ToolCallStep nor its ToolResultStep (which would echo the
        # payload as free text) surfaces — the payload arrives exactly once as the
        # validated terminal StructuredFinal.
        assert not any(isinstance(event, ToolCallStep) for event in events)
        assert not any(isinstance(event, ToolResultStep) for event in events)
        finals = [event for event in events if isinstance(event, StructuredFinal)]
        assert [final.data for final in finals] == [{"value": 7}]

    def test_stream_projection_suppresses_a_union_variants_synthetic_tool_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A union ``response_format`` binds one synthetic tool per variant; the model
        # answering through the ``_Cat`` variant must NOT leak its ToolCall/ToolResult
        # (the result echoes the payload as free text) — the payload arrives once as
        # the terminal StructuredFinal.
        model = RecordingChatModel(
            [AIMessage(content="", tool_calls=[{"id": "call_1", "name": "_Cat", "args": {"meow": 7}}])]
        )
        _seams(monkeypatch, model, InMemorySaver())

        async def collect() -> list[Any]:
            return [
                event
                async for event in astream_tools_agent_events(
                    "sys", ["hi"], [], config=_config("t-stream-union"), response_format=_Cat | _Dog
                )
            ]

        events = asyncio.run(collect())

        assert not any(isinstance(event, ToolCallStep) for event in events)
        assert not any(isinstance(event, ToolResultStep) for event in events)
        finals = [event for event in events if isinstance(event, StructuredFinal)]
        assert [final.data for final in finals] == [_Cat(meow=7)]

    def test_stream_projection_suppresses_a_titled_oneof_variants_synthetic_tool_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A ``oneOf`` dict ``response_format`` binds one synthetic tool per variant; the
        # model answering through the ``A`` variant must NOT leak its ToolCall/ToolResult
        # — the payload arrives once as the terminal StructuredFinal.
        schema = {
            "title": "Top",
            "oneOf": [
                {"title": "A", "type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]},
                {"title": "B", "type": "object", "properties": {"b": {"type": "integer"}}, "required": ["b"]},
            ],
        }
        model = RecordingChatModel(
            [AIMessage(content="", tool_calls=[{"id": "call_1", "name": "A", "args": {"a": 7}}])]
        )
        _seams(monkeypatch, model, InMemorySaver())

        async def collect() -> list[Any]:
            return [
                event
                async for event in astream_tools_agent_events(
                    "sys", ["hi"], [], config=_config("t-stream-oneof"), response_format=schema
                )
            ]

        events = asyncio.run(collect())

        assert not any(isinstance(event, ToolCallStep) for event in events)
        assert not any(isinstance(event, ToolResultStep) for event in events)
        finals = [event for event in events if isinstance(event, StructuredFinal)]
        assert [final.data for final in finals] == [{"a": 7}]

    def test_one_strategy_object_is_shared_by_graph_and_projection(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The schema is wrapped ONCE: the very ``ToolStrategy`` the graph is compiled
        # with is the one the projection derives its suppressed names from. Asserting
        # object IDENTITY is what makes an untitled-variant ``oneOf`` correct — two
        # independent wraps would draw two disjoint random ``response_format_<hex>``
        # name sets, so the suppression could only match by identity, never by value.
        schema = {"title": "Top", "oneOf": [{"type": "object"}, {"type": "object"}]}
        bound: dict[str, Any] = {}
        projected: dict[str, Any] = {}

        real_create_agent = bta.create_agent

        def spy_create_agent(*args: Any, **kwargs: Any) -> Any:
            bound["strategy"] = kwargs["response_format"]
            return real_create_agent(*args, **kwargs)

        real_names = stream_events._structured_tool_names

        def spy_names(strategy: Any) -> Any:
            projected["strategy"] = strategy
            return real_names(strategy)

        monkeypatch.setattr(bta, "create_agent", spy_create_agent)
        monkeypatch.setattr(stream_events, "_structured_tool_names", spy_names)

        model = RecordingChatModel([AIMessage(content="done")])
        _seams(monkeypatch, model, InMemorySaver())

        async def drain() -> None:
            async for _ in astream_tools_agent_events(
                "sys", ["hi"], [], config=_config("t-identity"), response_format=schema
            ):
                pass

        asyncio.run(drain())

        assert isinstance(bound["strategy"], ToolStrategy)
        assert projected["strategy"] is bound["strategy"]
