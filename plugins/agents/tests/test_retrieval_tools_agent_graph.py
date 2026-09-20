"""``retrieval_tools_agent`` registration, should-continue routing, and graph
wiring.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, RemoveMessage
from langgraph.constants import END
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from tai42_contract.agent import (
    Agent,
)
from tai42_contract.app import tai42_app
from tests._retrieval_tools_agent_support import (
    AGENT_NAME,
    _graph,
)

from tai42_agents.retrieval_tools_agent import graph as rgraph
from tai42_agents.retrieval_tools_agent.agent import (
    RetrievalToolsAgent,
    RetrievalToolsAgentInput,
)
from tai42_agents.retrieval_tools_agent.graph import RetrievalToolsGraph


class TestRegistration:
    def test_decorator_registers_a_live_instance(self) -> None:
        agent = tai42_app.agents.get_agent(AGENT_NAME)
        assert isinstance(agent, RetrievalToolsAgent)
        assert isinstance(agent, Agent)

    def test_tool_metadata(self) -> None:
        assert RetrievalToolsAgent.tool_name == AGENT_NAME
        assert RetrievalToolsAgent.tool_description
        # The tool-face input model follows the ``<AgentClass>Input`` convention.
        assert RetrievalToolsAgent.ToolInput is RetrievalToolsAgentInput
        # Live tools are an API-only astream input, never in the JSON tool schema.
        assert "tools" not in RetrievalToolsAgent.ToolInput.model_fields


class TestShouldContinueRouting:
    def _decide(self, agent: RetrievalToolsGraph, last: BaseMessage) -> Any:
        node = agent.should_continue_node()
        return node({"messages": [last]}, store=MagicMock())

    def test_continue_status_routes_through_context_node(self) -> None:
        agent = _graph()
        decision = self._decide(agent, AIMessage(content=json.dumps({"status": "continue"})))
        assert decision == agent.context_node_name
        assert decision != agent.agent_node_name

    def test_success_status_ends(self) -> None:
        agent = _graph()
        assert self._decide(agent, AIMessage(content=json.dumps({"status": "success"}))) == END

    def test_error_status_ends(self) -> None:
        agent = _graph()
        assert self._decide(agent, AIMessage(content=json.dumps({"status": "error"}))) == END

    def test_success_status_in_list_content_ends(self) -> None:
        # A provider that returns content blocks (list) instead of a bare string:
        # the routing node concatenates the text blocks before parsing the JSON.
        agent = _graph()
        last = AIMessage(content=[{"type": "text", "text": json.dumps({"status": "success", "result": "ok"})}])
        assert self._decide(agent, last) == END

    def test_success_status_in_bare_string_list_content_ends(self) -> None:
        # Some providers return a list of bare strings rather than typed blocks;
        # the routing node concatenates those too before parsing the JSON.
        agent = _graph()
        last = AIMessage(content=[json.dumps({"status": "success", "result": "ok"})])
        assert self._decide(agent, last) == END

    def test_pending_tool_calls_dispatch_to_execute(self) -> None:
        agent = _graph()
        last = AIMessage(content="", tool_calls=[{"id": "t1", "name": "foo", "args": {}}])
        sends = self._decide(agent, last)
        assert [s.node for s in sends] == [agent.execute_tools_node_name]

    def test_retrieve_tools_call_dispatches_to_select(self) -> None:
        agent = _graph()
        last = AIMessage(content="", tool_calls=[{"id": "r1", "name": agent.retrieve_tools_tool_name, "args": {}}])
        sends = self._decide(agent, last)
        assert [s.node for s in sends] == [agent.select_tools_node_name]

    def test_mixed_calls_dispatch_to_both_tool_nodes(self) -> None:
        agent = _graph()
        last = AIMessage(
            content="",
            tool_calls=[
                {"id": "r1", "name": agent.retrieve_tools_tool_name, "args": {}},
                {"id": "t1", "name": "foo", "args": {}},
            ],
        )
        sends = self._decide(agent, last)
        assert sorted(s.node for s in sends) == sorted([agent.select_tools_node_name, agent.execute_tools_node_name])


class TestMalformedTerminalRaises:
    def _decide(self, last: BaseMessage) -> Any:
        node = _graph().should_continue_node()
        return node({"messages": [last]}, store=MagicMock())

    def test_non_json_terminal_raises(self) -> None:
        with pytest.raises(ValueError, match="not valid status JSON"):
            self._decide(AIMessage(content="all done!"))

    def test_empty_terminal_raises(self) -> None:
        with pytest.raises(ValueError, match="no text content"):
            self._decide(AIMessage(content=""))

    def test_non_object_json_terminal_raises(self) -> None:
        with pytest.raises(ValueError, match="not a JSON object"):
            self._decide(AIMessage(content="5"))

    def test_unknown_status_raises(self) -> None:
        with pytest.raises(ValueError, match="status must be one of"):
            self._decide(AIMessage(content=json.dumps({"status": "weird"})))


class TestGraphWiring:
    def test_every_loop_back_routes_through_context(self) -> None:
        builder = _graph().build()
        assert ("__start__", "context") in builder.edges
        assert ("context", "agent") in builder.edges
        assert ("execute_tools", "context") in builder.edges
        assert ("select_tools", "context") in builder.edges
        assert ("execute_tools", "agent") not in builder.edges
        assert ("select_tools", "agent") not in builder.edges


class TestContextNode:
    def _run(self, agent: RetrievalToolsGraph, state: dict[str, Any]) -> Any:
        afunc = agent.context_node().afunc
        assert afunc is not None

        async def _invoke() -> Any:
            return await afunc(state, None, store=MagicMock())

        return asyncio.run(_invoke())

    def test_no_reduction_returns_empty_update(self) -> None:
        agent = _graph()
        with patch.object(rgraph, "areduce_context", new=AsyncMock(return_value=None)):
            out = self._run(agent, {"messages": [HumanMessage("hi", id="1")]})
        assert out == {}

    def test_reduction_wraps_with_remove_all(self) -> None:
        agent = _graph()
        reduced = [HumanMessage("summary", id="s")]
        with patch.object(rgraph, "areduce_context", new=AsyncMock(return_value=reduced)):
            out = self._run(agent, {"messages": [HumanMessage("hi", id="1")]})
        messages = out["messages"]
        assert isinstance(messages[0], RemoveMessage)
        assert messages[0].id == REMOVE_ALL_MESSAGES
        assert messages[1:] == reduced
