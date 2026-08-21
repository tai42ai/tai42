"""Agent async ``ask_user`` park/resume over a REAL deep-agent graph.

Every test drives a real ``build_deep_agent`` graph with a scripted fake chat model and
an in-memory checkpointer/store, so the ``AsyncParkMiddleware`` before-model hook, the
messages reducer, and the interrupt/resume routing land exactly as a live run — no LLM,
no network. Async is driven with ``asyncio.run`` (the repo does not use pytest-asyncio).

The park marker a tool returns here is the same reserved contract marker the in-process
client-tool seam stamps onto an async ``ask_user``'s ``SuspendedInteraction`` sentinel;
the tool body counts its invocations so a resume that re-ran it (a double-park) is caught.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from langgraph.types import Command
from pydantic import PrivateAttr
from tai42_contract.interactions import EXPIRY_ANSWER, suspended_interaction_marker

from tai42_agents._internal.park.driver import _collect_pending_interrupts, _park_interactions
from tai42_agents._internal.park.middleware import AsyncParkMiddleware
from tai42_agents.deep_agent.factory import build_deep_agent


class ScriptedChatModel(BaseChatModel):
    """Emits a fixed list of AIMessages in order; ``bind_tools`` is a no-op so the graph
    binds its tools and stays scripted. Shared across a main agent and its subagents, so a
    nested run consumes the next scripted response in call order."""

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


class _CountingAsk:
    """A tool that parks: returns the reserved async-park marker and counts each call, so a
    resume that re-executed the tool (a double-park) shows a second call."""

    def __init__(self, interaction_id: str, expiry_at: Any = None) -> None:
        self.calls = 0
        self._interaction_id = interaction_id
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            self.calls += 1
            return suspended_interaction_marker(self._interaction_id, self._expiry_at)

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _build(model: BaseChatModel, tools: list[StructuredTool], **kwargs: Any) -> Any:
    return asyncio.run(
        build_deep_agent(
            llm=model,
            store=InMemoryStore(),
            checkpointer=InMemorySaver(),
            tools=tools,
            **kwargs,
        )
    )


def _ask_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "ask", "args": {}}])


def _park_interrupt(snapshot: Any) -> tuple[str, dict[str, Any]]:
    """The single pending park interrupt's ``(id, interactions)`` from a snapshot."""
    parks = [
        (iid, interactions)
        for iid, value in _collect_pending_interrupts(snapshot)
        if (interactions := _park_interactions(value)) is not None
    ]
    assert len(parks) == 1, parks
    return parks[0]


def test_park_then_answer_runs_ask_exactly_once() -> None:
    ask = _CountingAsk("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    graph = _build(model, [ask.tool()])
    config = {"configurable": {"thread_id": "t-park"}}

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}, config))
    snapshot = asyncio.run(graph.aget_state(config, subgraphs=True))
    interrupt_id, interactions = _park_interrupt(snapshot)
    assert interactions == {"i1": None}
    assert ask.calls == 1

    final = asyncio.run(graph.ainvoke(Command(resume={interrupt_id: {"i1": "the answer"}}), config))
    # The tool ran once by construction; the resume substituted the answer, never re-ran it.
    assert ask.calls == 1
    assert final["messages"][-1].content == "all done"
    # The parked ToolMessage now carries the substituted answer, not the marker.
    tool_messages = [m for m in final["messages"] if getattr(m, "tool_call_id", None) == "c1"]
    assert tool_messages[-1].content == "the answer"


def test_park_then_expiry_feeds_the_expiry_marker() -> None:
    deadline = datetime(2030, 1, 1, tzinfo=UTC)
    ask = _CountingAsk("i1", expiry_at=deadline)
    model = ScriptedChatModel([_ask_call(), AIMessage(content="expired path")])
    graph = _build(model, [ask.tool()])
    config = {"configurable": {"thread_id": "t-expiry"}}

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}, config))
    snapshot = asyncio.run(graph.aget_state(config, subgraphs=True))
    interrupt_id, interactions = _park_interrupt(snapshot)
    assert interactions == {"i1": deadline.isoformat()}

    final = asyncio.run(graph.ainvoke(Command(resume={interrupt_id: {"i1": EXPIRY_ANSWER}}), config))
    tool_messages = [m for m in final["messages"] if getattr(m, "tool_call_id", None) == "c1"]
    # The expiry marker round-trips as the tool result content (JSON-serialized).
    assert '"tai42:interaction_expired"' in tool_messages[-1].content


def test_two_siblings_park_in_one_superstep_and_resume_together() -> None:
    ask1 = _CountingAsk("iA")

    def ask_a() -> dict[str, Any]:
        ask1.calls += 1
        return suspended_interaction_marker("iA", None)

    def ask_b() -> dict[str, Any]:
        return suspended_interaction_marker("iB", None)

    tools = [
        StructuredTool.from_function(ask_a, name="ask_a", description="a"),
        StructuredTool.from_function(ask_b, name="ask_b", description="b"),
    ]
    model = ScriptedChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "ca", "name": "ask_a", "args": {}},
                    {"id": "cb", "name": "ask_b", "args": {}},
                ],
            ),
            AIMessage(content="both answered"),
        ]
    )
    graph = _build(model, tools)
    config = {"configurable": {"thread_id": "t-siblings"}}

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}, config))
    snapshot = asyncio.run(graph.aget_state(config, subgraphs=True))
    interrupt_id, interactions = _park_interrupt(snapshot)
    # ONE interrupt for the whole super-step, carrying BOTH siblings.
    assert interactions == {"iA": None, "iB": None}

    final = asyncio.run(graph.ainvoke(Command(resume={interrupt_id: {"iA": "answer-a", "iB": "answer-b"}}), config))
    assert final["messages"][-1].content == "both answered"
    by_call = {m.tool_call_id: m.content for m in final["messages"] if getattr(m, "tool_call_id", None)}
    assert by_call["ca"] == "answer-a"
    assert by_call["cb"] == "answer-b"


def test_hitl_interrupt_and_async_park_coexist_by_id() -> None:
    # An interrupt_on approval on one tool and an async-ask park on another must not
    # collide: each resumes by its own id.
    ask = _CountingAsk("i1")
    approve = StructuredTool.from_function(lambda: "approved", name="approve", description="needs approval")
    model = ScriptedChatModel(
        [
            AIMessage(content="", tool_calls=[{"id": "cap", "name": "approve", "args": {}}]),
            _ask_call("c1"),
            AIMessage(content="done after both"),
        ]
    )
    graph = _build(model, [ask.tool(), approve], interrupt_on={"approve": True})
    config = {"configurable": {"thread_id": "t-coexist"}}

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}, config))
    # First pause: the HITL approval interrupt (not a park).
    snapshot = asyncio.run(graph.aget_state(config, subgraphs=True))
    pending = _collect_pending_interrupts(snapshot)
    assert all(_park_interactions(value) is None for _, value in pending)
    hitl_id = pending[0][0]

    # Approve → the model then calls ask → the run parks.
    asyncio.run(graph.ainvoke(Command(resume={hitl_id: {"decisions": [{"type": "approve"}]}}), config))
    snapshot = asyncio.run(graph.aget_state(config, subgraphs=True))
    park_id, interactions = _park_interrupt(snapshot)
    assert interactions == {"i1": None}
    assert park_id != hitl_id

    final = asyncio.run(graph.ainvoke(Command(resume={park_id: {"i1": "the answer"}}), config))
    assert final["messages"][-1].content == "done after both"


def test_park_middleware_is_the_leading_before_model_hook() -> None:
    # Probe gate 2: the park hook must precede any message-compacting hook. deepagents'
    # compactors (summarization, memory) run through wrap_model_call — during the model
    # node, skipped on a park super-step — and the park hook is the FIRST before_model
    # node in the merged stack, so a marked ToolMessage is never evicted before the park
    # is recognized. The stack order preserves the middleware list order, park-first.
    model = ScriptedChatModel([AIMessage(content="ok")])
    graph = _build(model, [])
    before_model_nodes = [name for name in graph.get_graph().nodes if name.endswith(".before_model")]
    assert before_model_nodes[0] == "AsyncParkMiddleware.before_model", before_model_nodes


def test_park_inside_a_subagent_propagates_and_resumes_by_id() -> None:
    # Probe gate 1: an async ask parked inside a subagent stack surfaces its interrupt id
    # (namespaced through the parent task tool) and resumes back into the subgraph by id.
    from tai42_agents.deep_agent.spec import ResolvedSubAgentSpec

    ask = _CountingAsk("i-sub")
    subagent = ResolvedSubAgentSpec(
        name="asker",
        description="asks the user",
        system_prompt="ask",
        tools=[ask.tool()],
    )
    # Call order across the shared model: main calls task(asker) → subagent calls ask
    # (parks) → [resume] → subagent finalizes → main finalizes.
    model = ScriptedChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "ct", "name": "task", "args": {"description": "ask them", "subagent_type": "asker"}}
                ],
            ),
            _ask_call("c1"),
            AIMessage(content="sub done"),
            AIMessage(content="main done"),
        ]
    )
    graph = _build(model, [], subagents=[subagent])
    config = {"configurable": {"thread_id": "t-subagent"}}

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}, config))
    snapshot = asyncio.run(graph.aget_state(config, subgraphs=True))
    interrupt_id, interactions = _park_interrupt(snapshot)
    assert interactions == {"i-sub": None}
    assert ask.calls == 1

    final = asyncio.run(graph.ainvoke(Command(resume={interrupt_id: {"i-sub": "sub answer"}}), config))
    assert ask.calls == 1
    assert final["messages"][-1].content == "main done"


def test_two_parallel_subagent_parks_surface_and_resume_by_id() -> None:
    # Two parallel task-tool subagents that each async-ask surface TWO DISTINCT park interrupts
    # in ONE parent super-step (empirically reachable). langgraph's resume map feeds each
    # interrupt its own answers in one resume, and both subagents finalize back to the parent.
    from tai42_agents.deep_agent.spec import ResolvedSubAgentSpec

    calls = {"n": 0}
    seq = ["iA", "iB"]

    def ask_seq() -> dict[str, Any]:
        interaction_id = seq[calls["n"]]
        calls["n"] += 1
        return suspended_interaction_marker(interaction_id, None)

    subagent = ResolvedSubAgentSpec(
        name="asker",
        description="asks the user",
        system_prompt="ask",
        tools=[StructuredTool.from_function(ask_seq, name="ask", description="ask")],
    )
    model = ScriptedChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "ta", "name": "task", "args": {"description": "A", "subagent_type": "asker"}},
                    {"id": "tb", "name": "task", "args": {"description": "B", "subagent_type": "asker"}},
                ],
            ),
            _ask_call("ca"),
            _ask_call("cb"),
            AIMessage(content="sub done"),
            AIMessage(content="sub done"),
            AIMessage(content="main done"),
        ]
    )
    graph = _build(model, [], subagents=[subagent])
    config = {"configurable": {"thread_id": "t-multipark"}}

    asyncio.run(graph.ainvoke({"messages": [HumanMessage(content="go")]}, config))
    snapshot = asyncio.run(graph.aget_state(config, subgraphs=True))
    parks = [
        (iid, interactions)
        for iid, value in _collect_pending_interrupts(snapshot)
        if (interactions := _park_interactions(value)) is not None
    ]
    # TWO distinct park interrupts, each carrying its own single interaction.
    assert len(parks) == 2, parks
    by_interaction = {next(iter(interactions)): iid for iid, interactions in parks}
    assert set(by_interaction) == {"iA", "iB"}
    assert calls["n"] == 2

    resume_map = {by_interaction["iA"]: {"iA": "answer-a"}, by_interaction["iB"]: {"iB": "answer-b"}}
    final = asyncio.run(graph.ainvoke(Command(resume=resume_map), config))
    assert final["messages"][-1].content == "main done"


def test_scan_ignores_non_park_tool_results() -> None:
    # A plain (non-park) tool result carries no marker, so no interrupt fires.
    from langchain_core.messages import ToolMessage

    messages = [
        AIMessage(content="", tool_calls=[{"id": "c1", "name": "t", "args": {}}]),
        ToolMessage(content="plain", tool_call_id="c1", id="m1"),
    ]
    assert AsyncParkMiddleware().before_model({"messages": messages}, None) is None


def test_marker_round_trips_through_msg_content_output() -> None:
    # Probe gate 3: the ToolNode serializes a tool's dict return through
    # msg_content_output before it becomes ToolMessage.content; the reserved marker must
    # survive that serialization so the park hook recognizes it off the message content.
    from langgraph.prebuilt.tool_node import msg_content_output
    from tai42_contract.interactions import read_suspended_interaction_marker

    for expiry in (None, datetime(2030, 1, 1, tzinfo=UTC)):
        marker = suspended_interaction_marker("i1", expiry)
        serialized = msg_content_output(marker)
        # The dict marker serializes to a JSON string content, and parses back intact.
        assert isinstance(serialized, str)
        assert read_suspended_interaction_marker(serialized) == {
            "interaction_id": "i1",
            "expiry_at": expiry.isoformat() if expiry else None,
        }
