"""``retrieval_tools_agent`` node behaviours: build, namespace, tool selection,
stale-id handling, and the terminal-result helper.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool, ToolException
from tests._retrieval_tools_agent_support import (
    RecordingStore,
    StubStore,
    _graph,
    _tool,
)

from tai42_agents.retrieval_tools_agent import graph as rgraph
from tai42_agents.retrieval_tools_agent.agent import (
    _terminal_result,
)
from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph


class TestAbuild:
    def test_abuild_raises_without_store(self) -> None:
        agent = RetrievalToolsGraph(tools=[], llm=MagicMock(), store=None)
        with pytest.raises(ValueError, match="requires a store"):
            asyncio.run(agent.abuild())


class TestNamespace:
    def test_namespace_is_per_tool_set_and_deterministic(self) -> None:
        g1 = RetrievalToolsGraph(tools=[_tool("a"), _tool("b")], llm=MagicMock())
        g2 = RetrievalToolsGraph(tools=[_tool("b"), _tool("a")], llm=MagicMock())
        g3 = RetrievalToolsGraph(tools=[_tool("a"), _tool("c")], llm=MagicMock())

        assert g1.namespace[0] == "tools"
        assert len(g1.namespace) == 2
        # Same tool-set (any input order) -> same namespace, so the embedding
        # cache still reuses across identical runs.
        assert g1.namespace == g2.namespace
        # A different tool-set -> a different namespace, so the indexes don't
        # share one global bucket.
        assert g1.namespace != g3.namespace

    def test_add_to_store_uses_the_tool_set_namespace(self) -> None:
        agent = _graph()
        store = RecordingStore()
        asyncio.run(agent.add_to_store(store, "k1", _tool("foo", "does foo")))  # type: ignore[arg-type]
        assert store.puts == [(agent.namespace, "k1", {"description": "foo: does foo"})]

    def test_overwrite_store_deletes_before_put(self) -> None:
        agent = _graph(overwrite_store=True)
        store = RecordingStore()
        asyncio.run(agent.add_to_store(store, "k1", _tool("foo", "does foo")))  # type: ignore[arg-type]
        assert store.deletes == [(agent.namespace, "k1")]
        assert store.puts == [(agent.namespace, "k1", {"description": "foo: does foo"})]

    def test_retrieve_tool_searches_with_the_tool_set_namespace(self) -> None:
        agent = _graph(tools_limit=5)
        store = RecordingStore()
        tool = agent.retrieve_tools_tool()
        out = asyncio.run(tool.ainvoke({"query": "find", "store": store}))
        assert out == ["id1", "id2"]
        assert store.searches == [(agent.namespace, "find", 5)]


class TestMergedSelectedTools:
    def test_dedups_within_right_and_against_left(self) -> None:
        # ``b`` repeats within ``right`` and ``a`` already sits in ``left``: each
        # survives once, so a tool is never bound twice.
        assert rgraph._merged_selected_tools(["a"], ["b", "a", "b", "c", "b"]) == ["a", "b", "c"]

    def test_preserves_first_seen_order(self) -> None:
        assert rgraph._merged_selected_tools([], ["c", "a", "b", "a", "c"]) == ["c", "a", "b"]


class TestSelectToolsNode:
    def _run(self, agent: RetrievalToolsGraph, tool_calls: list[dict], store: Any) -> Any:
        afunc = agent.select_tools_node().afunc
        assert afunc is not None

        async def _invoke() -> Any:
            return await afunc(tool_calls, None, store=store)

        return asyncio.run(_invoke())

    def test_success_reports_available_tools_ids_and_labels_message(self) -> None:
        agent = _graph()
        agent.tool_registry = {"id1": _tool("alpha"), "id2": _tool("beta")}
        store = RecordingStore()
        out = self._run(agent, [{"id": "c1", "name": "retrieve_tools", "args": {"query": "x"}}], store)
        assert out["selected_tool_ids"] == ["id1", "id2"]
        message = out["messages"][0]
        assert isinstance(message, ToolMessage)
        assert message.tool_call_id == "c1"
        # The result message is labeled with the retrieve tool's name and is not
        # flagged as an error.
        assert message.name == "retrieve_tools"
        assert message.status != "error"
        assert "alpha" in message.content
        assert "beta" in message.content

    def test_tool_logic_error_becomes_labeled_error_tool_message(self) -> None:
        agent = _graph()

        class BoomStore(RecordingStore):
            async def asearch(self, *args: Any, **kwargs: Any) -> Any:
                raise ToolException("boom")

        out = self._run(agent, [{"id": "c1", "name": "retrieve_tools", "args": {"query": "x"}}], BoomStore())
        assert out["selected_tool_ids"] == []
        message = out["messages"][0]
        assert message.name == "retrieve_tools"
        assert message.status == "error"
        assert message.content.startswith("Error:")
        assert "boom" in message.content

    def test_infrastructure_error_propagates(self) -> None:
        # A store/embedding/connection failure is NOT a tool-logic signal — it
        # propagates loudly instead of being masked as a ToolMessage.
        agent = _graph()

        class OutageStore(RecordingStore):
            async def asearch(self, *args: Any, **kwargs: Any) -> Any:
                raise ConnectionError("vector store unreachable")

        with pytest.raises(ConnectionError):
            self._run(agent, [{"id": "c1", "name": "retrieve_tools", "args": {"query": "x"}}], OutageStore())

    def test_programming_error_after_retrieval_propagates(self) -> None:
        # The except is narrowed to the retrieve invocation, so a fault in the
        # id->name mapping (here: a non-iterable retrieval result) is NOT masked
        # as a ToolMessage — it propagates.
        agent = _graph()

        async def _bad(query: str, *, store: Any) -> Any:
            """Return a non-iterable so the id->name mapping faults."""
            return 123

        agent._retrieve_tools_tool = StructuredTool.from_function(func=None, coroutine=_bad, name="retrieve_tools")
        with pytest.raises(TypeError):
            self._run(agent, [{"id": "c1", "name": "retrieve_tools", "args": {"query": "x"}}], RecordingStore())

    def test_unregistered_retrieved_id_raises(self) -> None:
        # A retrieved id addresses this tool-set's own namespace, so a miss is a
        # real inconsistency and raises rather than being silently dropped.
        agent = _graph()
        with pytest.raises(KeyError, match="no entry in this tool-set"):
            self._run(agent, [{"id": "c1", "name": "retrieve_tools", "args": {"query": "x"}}], StubStore(["ghost"]))


class TestAgentNodeStaleId:
    def test_unregistered_selected_id_raises(self) -> None:
        agent = _graph()
        afunc = agent.agent_node().afunc
        assert afunc is not None

        async def _invoke() -> Any:
            return await afunc(
                {"messages": [HumanMessage("hi")], "selected_tool_ids": ["ghost"]}, None, store=MagicMock()
            )

        with pytest.raises(KeyError, match="no entry in this tool-set"):
            asyncio.run(_invoke())


class TestTerminalResultHelper:
    def test_extracts_result_from_success_envelope(self) -> None:
        assert _terminal_result(json.dumps({"status": "success", "message": "m", "result": "ok"})) == "ok"

    def test_takes_last_of_concatenated_envelopes(self) -> None:
        text = json.dumps({"status": "continue", "message": "m", "result": None}) + json.dumps(
            {"status": "success", "message": "done", "result": "final"}
        )
        assert _terminal_result(text) == "final"

    def test_error_envelope_with_null_result_is_empty_string(self) -> None:
        assert _terminal_result(json.dumps({"status": "error", "message": "x", "result": None})) == ""

    def test_non_string_result_is_stringified(self) -> None:
        assert _terminal_result(json.dumps({"status": "success", "message": "m", "result": 7})) == "7"

    def test_non_json_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid status JSON"):
            _terminal_result("all done")

    def test_empty_text_raises(self) -> None:
        with pytest.raises(ValueError, match="no status JSON content"):
            _terminal_result("   ")

    def test_non_object_raises(self) -> None:
        with pytest.raises(ValueError, match="not a JSON object"):
            _terminal_result("5")

    def test_unknown_status_raises(self) -> None:
        with pytest.raises(ValueError, match="status must be success/error"):
            _terminal_result(json.dumps({"status": "continue", "result": "x"}))

    def test_missing_result_field_raises(self) -> None:
        with pytest.raises(ValueError, match="no 'result' field"):
            _terminal_result(json.dumps({"status": "success", "message": "m"}))
