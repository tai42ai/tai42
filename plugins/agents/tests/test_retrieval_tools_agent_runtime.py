"""``retrieval_tools_agent`` runtime: per-run system prompt, rolling cache marks,
stream events, terminal-result surfacing, and embedding-dims caching.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import ToolException
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.constants import START
from tai42_contract.agent import (
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    RunUsage,
    ToolCallStep,
    ToolResultStep,
)
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.string_util import text_to_md5
from tests._retrieval_tools_agent_support import (
    ScriptedChatModel,
    StubStore,
    _collect,
    _mark_count,
    _marked_user,
    _tool,
)

from tai42_agents._internal.stream_events import aproject_agent_events
from tai42_agents.retrieval_tools_agent import agent as ragent
from tai42_agents.retrieval_tools_agent.agent import (
    RetrievalToolsAgent,
    _embedding_dims,
    _embedding_dims_cache,
)
from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph


class TestSystemPromptPerRun:
    def _compiled(self, llm: ScriptedChatModel, checkpoint: InMemorySaver) -> Any:
        alpha = _tool("alpha", "the alpha tool")
        alpha_id = text_to_md5("alpha")
        return asyncio.run(
            RetrievalToolsGraph(
                tools=[alpha],
                llm=llm,
                store=StubStore([alpha_id]),
                checkpoint=checkpoint,
                system_prompt="be brief",
            ).abuild()
        )

    def _terminal_llm(self) -> ScriptedChatModel:
        return ScriptedChatModel(
            [AIMessage(content=json.dumps({"status": "success", "message": "done", "result": "ok"}))]
        )

    def test_system_prompt_is_prepended_at_the_model_call_but_never_stored(self) -> None:
        llm = self._terminal_llm()
        graph = self._compiled(llm, InMemorySaver())
        config = {"configurable": {"thread_id": "sys-t1"}}

        state = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "hi"}]}, config))

        # The model call led with exactly one system message — the per-run prompt.
        seen = llm._seen[0]
        assert isinstance(seen[0], SystemMessage)
        assert seen[0].content == "be brief"
        assert sum(isinstance(m, SystemMessage) for m in seen) == 1
        # The checkpointed thread stays system-free: conversation only.
        assert not any(isinstance(m, SystemMessage) for m in state["messages"])

    def test_stored_system_message_is_purged_before_the_model_call(self) -> None:
        llm = self._terminal_llm()
        graph = self._compiled(llm, InMemorySaver())
        config = {"configurable": {"thread_id": "sys-t2"}}

        # Seed a thread whose stored history carries a system message ahead of
        # the conversation, written straight into the checkpoint.
        asyncio.run(
            graph.aupdate_state(
                config,
                {
                    "messages": [
                        SystemMessage(content="stored rules", id="stale-system"),
                        HumanMessage(content="earlier turn", id="h1"),
                    ]
                },
                as_node=START,
            )
        )
        state = asyncio.run(graph.ainvoke({"messages": [{"role": "user", "content": "hi"}]}, config))

        # The context node purged the stored system message, so the model saw
        # exactly one — the per-run prompt, first — never the stored one.
        seen = llm._seen[0]
        systems = [m for m in seen if isinstance(m, SystemMessage)]
        assert len(systems) == 1
        assert seen[0] is systems[0]
        assert systems[0].content == "be brief"
        # The purge is persistent: the stored system message is gone from state
        # while the conversation survives.
        assert not any(isinstance(m, SystemMessage) for m in state["messages"])
        assert [m.content for m in state["messages"][:2]] == ["earlier turn", "hi"]


class TestRollingCacheMark:
    def test_agent_node_rolls_accumulated_cache_marks_on_a_reused_thread(self) -> None:
        # Every turn marks its user message; the marks persist into the reused
        # thread's history. The agent node runs no wrap_model_call middleware (its
        # context pipeline goes through areduce_context, which does not honor it), so
        # without the explicit roll the second turn's model call would carry two
        # user-side breakpoints and grow unbounded. The node's explicit
        # ``roll_cache_marks`` strips every older mark, so each model call sends
        # exactly one — the newest — while the checkpointed thread keeps both.
        alpha = _tool("alpha", "the alpha tool")
        alpha_id = text_to_md5("alpha")
        llm = ScriptedChatModel(
            [
                AIMessage(content=json.dumps({"status": "success", "message": "done", "result": "one"})),
                AIMessage(content=json.dumps({"status": "success", "message": "done", "result": "two"})),
            ]
        )
        graph = asyncio.run(
            RetrievalToolsGraph(
                tools=[alpha], llm=llm, store=StubStore([alpha_id]), checkpoint=InMemorySaver()
            ).abuild()
        )
        config = {"configurable": {"thread_id": "roll-t"}}

        asyncio.run(graph.ainvoke(_marked_user("first"), config))
        asyncio.run(graph.ainvoke(_marked_user("second"), config))

        # First model call: only the turn-1 user mark exists — inert, one breakpoint.
        assert _mark_count(llm._seen[0]) == 1
        # Second model call: history holds turn-1 + turn-2 user marks; the older one is
        # stripped, so the outgoing request carries exactly one user-side breakpoint.
        second_call = llm._seen[1]
        assert _mark_count(second_call) == 1
        # The surviving mark is the newest turn's user message, not the older one.
        newest_user = [m for m in second_call if isinstance(m, HumanMessage)][-1]
        assert newest_user.content == [{"type": "text", "text": "second", "cache_control": {"type": "ephemeral"}}]
        # The roll is request-scoped: the checkpointed thread still holds both marks, so
        # the next turn re-rolls from the same history rather than losing the record.
        snapshot = asyncio.run(graph.aget_state(config))
        assert _mark_count(snapshot.values.get("messages", [])) == 2


class TestStreamEvents:
    def test_full_run_emits_the_event_taxonomy(self) -> None:
        alpha = _tool("alpha", "the alpha tool")
        alpha_id = text_to_md5("alpha")

        llm = ScriptedChatModel(
            [
                AIMessage(
                    content="",
                    additional_kwargs={"reasoning_content": "thinking about alpha"},
                    tool_calls=[{"id": "r1", "name": "retrieve_tools", "args": {"query": "need alpha"}}],
                ),
                AIMessage(
                    content=json.dumps({"status": "success", "message": "done", "result": "ok"}),
                    usage_metadata={"input_tokens": 5, "output_tokens": 3, "total_tokens": 8},
                ),
            ]
        )
        graph = asyncio.run(RetrievalToolsGraph(tools=[alpha], llm=llm, store=StubStore([alpha_id])).abuild())

        messages = {"messages": [{"role": "user", "content": "do it"}]}
        config = {"configurable": {"thread_id": "t1"}}
        events = asyncio.run(_collect(aproject_agent_events(graph, messages, config)))

        kinds = [type(event) for event in events]
        assert ReasoningStep in kinds
        assert ToolCallStep in kinds
        assert ToolResultStep in kinds
        assert RunUsage in kinds
        assert MessageFinal in kinds

        tool_call = next(e for e in events if isinstance(e, ToolCallStep))
        assert tool_call.tool == "retrieve_tools"
        tool_result = next(e for e in events if isinstance(e, ToolResultStep))
        # The retrieve result step is labeled with the retrieve tool's name and
        # is not an error.
        assert tool_result.tool == "retrieve_tools"
        assert tool_result.is_error is False
        assert "alpha" in str(tool_result.result)
        usage = next(e for e in events if isinstance(e, RunUsage))
        assert usage.total_tokens == 8

    def test_retrieve_error_projects_a_labeled_error_tool_result(self) -> None:
        class BoomStore(StubStore):
            async def asearch(self, namespace_prefix: Any, /, **kwargs: Any) -> Any:
                raise ToolException("no match")

        llm = ScriptedChatModel(
            [
                AIMessage(content="", tool_calls=[{"id": "r1", "name": "retrieve_tools", "args": {"query": "q"}}]),
                AIMessage(content=json.dumps({"status": "error", "message": "gave up", "result": None})),
            ]
        )
        graph = asyncio.run(RetrievalToolsGraph(tools=[_tool("alpha", "a")], llm=llm, store=BoomStore([])).abuild())

        events = asyncio.run(
            _collect(
                aproject_agent_events(
                    graph, {"messages": [{"role": "user", "content": "x"}]}, {"configurable": {"thread_id": "t2"}}
                )
            )
        )
        tool_result = next(e for e in events if isinstance(e, ToolResultStep))
        assert tool_result.tool == "retrieve_tools"
        assert tool_result.is_error is True
        assert "Error:" in str(tool_result.result)


class TestTerminalResultSurfacing:
    def _agent_over_graph(self, monkeypatch: pytest.MonkeyPatch, graph: Any) -> RetrievalToolsAgent:
        async def fake_build(self: RetrievalToolsAgent, **kwargs: Any) -> tuple[Any, Any, Any, Any]:
            return (
                graph,
                {"messages": [{"role": "user", "content": "do it"}]},
                {"configurable": {"thread_id": "t1"}},
                None,
            )

        monkeypatch.setattr(RetrievalToolsAgent, "_build", fake_build)
        return RetrievalToolsAgent()

    def _two_step_graph(self, result: str) -> Any:
        alpha = _tool("alpha", "the alpha tool")
        alpha_id = text_to_md5("alpha")
        llm = ScriptedChatModel(
            [
                AIMessage(content="", tool_calls=[{"id": "r1", "name": "retrieve_tools", "args": {"query": "a"}}]),
                AIMessage(content=json.dumps({"status": "success", "message": "done", "result": result})),
            ]
        )
        return asyncio.run(RetrievalToolsGraph(tools=[alpha], llm=llm, store=StubStore([alpha_id])).abuild())

    def test_run_returns_terminal_result_not_the_envelope(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = self._agent_over_graph(monkeypatch, self._two_step_graph("THE RESULT"))
        assert asyncio.run(agent.run(user_message=TemplatedText(content="x"))) == "THE RESULT"

    def test_astream_final_is_result_and_no_envelope_leaks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        agent = self._agent_over_graph(monkeypatch, self._two_step_graph("THE RESULT"))
        events = asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="x"))))

        finals = [e for e in events if isinstance(e, MessageFinal)]
        assert len(finals) == 1
        assert finals[0].text == "THE RESULT"
        # Per-step / interim status JSON must not leak into the streamed answer.
        assert not any(isinstance(e, MessageDelta) for e in events)
        assert "status" not in finals[0].text

    def test_multi_step_run_surfaces_only_the_terminal_result(self, monkeypatch: pytest.MonkeyPatch) -> None:
        alpha = _tool("alpha", "the alpha tool")
        alpha_id = text_to_md5("alpha")
        llm = ScriptedChatModel(
            [
                AIMessage(content="", tool_calls=[{"id": "r1", "name": "retrieve_tools", "args": {"query": "a"}}]),
                # An interim "continue" step loops back through context; its status
                # JSON must not bleed into the final answer.
                AIMessage(content=json.dumps({"status": "continue", "message": "more", "result": None})),
                AIMessage(content=json.dumps({"status": "success", "message": "done", "result": "FINAL ANSWER"})),
            ]
        )
        graph = asyncio.run(RetrievalToolsGraph(tools=[alpha], llm=llm, store=StubStore([alpha_id])).abuild())
        agent = self._agent_over_graph(monkeypatch, graph)
        assert asyncio.run(agent.run(user_message=TemplatedText(content="x"))) == "FINAL ANSWER"

    def test_astream_raises_when_no_terminal_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_build(self: RetrievalToolsAgent, **kwargs: Any) -> tuple[Any, Any, Any, Any]:
            return "graph", "messages", "config", None

        async def fake_project(agent: Any, messages: Any, config: Any) -> AsyncIterator[Any]:
            yield ReasoningStep(text="thinking")

        monkeypatch.setattr(RetrievalToolsAgent, "_build", fake_build)
        monkeypatch.setattr(ragent, "aproject_agent_events", fake_project)
        agent = RetrievalToolsAgent()
        with pytest.raises(ValueError, match="produced no terminal message"):
            asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="x"))))

    def test_astream_suppresses_interim_status_deltas(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A live provider streams the per-step status envelope as ``MessageDelta``
        tokens; those are dropped so only the terminal ``result`` surfaces."""

        async def fake_build(self: RetrievalToolsAgent, **kwargs: Any) -> tuple[Any, Any, Any, Any]:
            return "graph", "messages", "config", None

        async def fake_project(agent: Any, messages: Any, config: Any) -> AsyncIterator[Any]:
            yield MessageDelta(text='{"status":"continue"')
            yield MessageDelta(text=',"result":null}')
            yield MessageFinal(text=json.dumps({"status": "success", "message": "done", "result": "THE RESULT"}))

        monkeypatch.setattr(RetrievalToolsAgent, "_build", fake_build)
        monkeypatch.setattr(ragent, "aproject_agent_events", fake_project)
        agent = RetrievalToolsAgent()
        events = asyncio.run(_collect(agent.astream(user_message=TemplatedText(content="x"))))

        assert not any(isinstance(e, MessageDelta) for e in events)
        finals = [e for e in events if isinstance(e, MessageFinal)]
        assert len(finals) == 1
        assert finals[0].text == "THE RESULT"


class TestEmbeddingDimsCache:
    def test_probes_once_then_serves_from_cache(self) -> None:
        _embedding_dims_cache.clear()
        embedding = MagicMock()
        embedding.aembed_query = AsyncMock(return_value=[0.0, 0.1, 0.2])

        first = asyncio.run(_embedding_dims("openai", embedding, None))
        second = asyncio.run(_embedding_dims("openai", embedding, None))

        assert first == second == 3
        # Second call is served from the cache — no second probe embed.
        embedding.aembed_query.assert_awaited_once()

    def test_distinct_kwargs_are_distinct_cache_keys(self) -> None:
        _embedding_dims_cache.clear()
        embedding = MagicMock()
        embedding.aembed_query = AsyncMock(side_effect=[[0.0, 0.1], [0.0, 0.1, 0.2, 0.3]])

        a = asyncio.run(_embedding_dims("openai", embedding, {"model": "small"}))
        b = asyncio.run(_embedding_dims("openai", embedding, {"model": "large"}))

        assert (a, b) == (2, 4)
        assert embedding.aembed_query.await_count == 2

    def test_lru_evicts_oldest_past_the_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With the bound forced to 2, a third distinct key evicts the oldest,
        the cache never exceeds the bound, and re-probing the evicted key runs a
        fresh embed."""
        _embedding_dims_cache.clear()
        monkeypatch.setattr(ragent, "agents_limits_settings", lambda: SimpleNamespace(embedding_dims_cache_size=2))
        embedding = MagicMock()
        # k1 -> 1 dim, k2 -> 2 dims, k3 -> 3 dims, then k1 re-probes -> 1 dim.
        embedding.aembed_query = AsyncMock(side_effect=[[0.0], [0.0, 0.0], [0.0, 0.0, 0.0], [0.0]])

        asyncio.run(_embedding_dims("p", embedding, {"model": "k1"}))
        asyncio.run(_embedding_dims("p", embedding, {"model": "k2"}))
        asyncio.run(_embedding_dims("p", embedding, {"model": "k3"}))

        assert len(_embedding_dims_cache) == 2
        # k1 was the oldest, so it was evicted; a fourth call re-probes it.
        again = asyncio.run(_embedding_dims("p", embedding, {"model": "k1"}))
        assert again == 1
        assert embedding.aembed_query.await_count == 4
        assert len(_embedding_dims_cache) == 2

    def test_hit_refreshes_recency(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cache hit on the oldest key moves it to most-recent, so the NEXT
        insert evicts a different key — never exceeding the bound."""
        _embedding_dims_cache.clear()
        monkeypatch.setattr(ragent, "agents_limits_settings", lambda: SimpleNamespace(embedding_dims_cache_size=2))
        embedding = MagicMock()
        embedding.aembed_query = AsyncMock(side_effect=[[0.0], [0.0, 0.0], [0.0, 0.0, 0.0]])

        asyncio.run(_embedding_dims("p", embedding, {"model": "k1"}))
        asyncio.run(_embedding_dims("p", embedding, {"model": "k2"}))
        # Hit on k1 refreshes its recency, so k2 is now the oldest.
        asyncio.run(_embedding_dims("p", embedding, {"model": "k1"}))
        # Inserting k3 evicts k2, leaving k1 (still cached — no re-probe).
        asyncio.run(_embedding_dims("p", embedding, {"model": "k3"}))

        assert len(_embedding_dims_cache) == 2
        # k1 survived: serving it takes no further probe.
        served = asyncio.run(_embedding_dims("p", embedding, {"model": "k1"}))
        assert served == 1
        assert embedding.aembed_query.await_count == 3
