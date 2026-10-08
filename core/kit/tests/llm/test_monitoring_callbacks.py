"""The kit monitoring callback handler over synthetic LangGraph graphs (neutral consumers, no engine).

Every graph runs over the real ``OtelWriter`` and an in-memory exporter, read back through
``InMemoryOtelReader``. The "resolved equals real" proofs capture, in a second inline handler
registered after the monitoring one, the very ``inputs`` / ``outputs`` object each chain
callback receives (encoded at that instant), then resolve the chain record's references
through the reader and compare.
"""

from __future__ import annotations

import dataclasses
import operator
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Annotated, Any, TypedDict, cast
from uuid import UUID

import orjson
import pytest
from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Send, interrupt
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel, PrivateAttr
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import MonitoringObservation, SpanKind, StepRole

from tai42_kit.llm.monitoring_callbacks import MonitoringCallbackHandler, _shallow_members, declared_chain_payload
from tai42_kit.llm.run_trace import bind_run_trace
from tai42_kit.monitoring import encode_payload, is_payload_ref, resolve_refs
from tai42_kit.monitoring.otel import OtelWriter
from tai42_kit.monitoring.testing import InMemoryOtelReader, span_to_observation

_FILLER = 4096


def _answer(n: int) -> str:
    return f"ANSWER-{n}-" + "a" * _FILLER


def _result(n: int) -> str:
    return f"RESULT-{n}-" + "r" * _FILLER


# --- the recording stack -----------------------------------------------------------------------


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def writer(monkeypatch: pytest.MonkeyPatch, exporter: InMemorySpanExporter) -> Iterator[OtelWriter]:
    monkeypatch.setenv("OTEL_PYTHON_SDK_INTERNAL_METRICS_ENABLED", "true")
    w = OtelWriter(exporter=exporter)
    app = SimpleNamespace(monitoring=SimpleNamespace(active=SimpleNamespace(writer=w)))
    with tai42_app.bound(app):
        yield w
    w.shutdown()


def _observations(writer: OtelWriter, exporter: InMemorySpanExporter) -> list[MonitoringObservation]:
    writer.flush()
    return [span_to_observation(s) for s in exporter.get_finished_spans()]


def _spans(writer: OtelWriter, exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    writer.flush()
    return list(exporter.get_finished_spans())


def _sends_as_dicts(value: Any) -> Any:
    if isinstance(value, Send):
        return {"node": value.node, "arg": _sends_as_dicts(value.arg), "timeout": None}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _sends_as_dicts(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {k: _sends_as_dicts(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sends_as_dicts(v) for v in value]
    return value


class _Capture(BaseCallbackHandler):
    """Captures each chain run's real input/output (encoded at the callback) and its span id."""

    run_inline = True

    def __init__(self, writer: OtelWriter) -> None:
        self.writer = writer
        self.span_of: dict[UUID, str] = {}
        self.name_of: dict[UUID, str] = {}
        self.inputs: dict[str, Any] = {}
        self.outputs: dict[str, Any] = {}
        self.node_spans: dict[str, list[str]] = {}

    def on_chain_start(
        self, serialized: Any, inputs: Any, *, run_id: UUID, metadata: Any = None, **kwargs: Any
    ) -> None:
        span_id = self.writer.current_span_id()
        assert span_id is not None
        self.span_of[run_id] = span_id
        self.name_of[run_id] = kwargs.get("name") or ""
        self.inputs[span_id] = orjson.loads(encode_payload(_sends_as_dicts(inputs)))

    def on_chain_end(self, outputs: Any, *, run_id: UUID, **kwargs: Any) -> None:
        self.outputs[self.span_of[run_id]] = orjson.loads(encode_payload(_sends_as_dicts(outputs)))


def _cfg(config: dict[str, Any]) -> RunnableConfig:
    return cast("RunnableConfig", config)


def _bound_config(writer: OtelWriter, **options: Any) -> tuple[RunnableConfig, _Capture]:
    config = bind_run_trace({"configurable": {"thread_id": "t"}}, **options).config
    capture = _Capture(writer)
    config["callbacks"] = [*config["callbacks"], capture]
    return _cfg(config), capture


async def _assert_resolved_equals_real(
    observations: list[MonitoringObservation], capture: _Capture, exporter: InMemorySpanExporter
) -> list[MonitoringObservation]:
    reader = InMemoryOtelReader(exporter)
    chains = [o for o in observations if o.type == SpanKind.CHAIN.value]
    assert chains
    for record in chains:
        assert record.trace_id is not None
        assert record.id in capture.inputs, record.name
        resolved_in = await resolve_refs(record.input, reader, trace_id=record.trace_id)
        assert resolved_in == capture.inputs[record.id], record.name
        if record.id in capture.outputs:
            resolved_out = await resolve_refs(record.output, reader, trace_id=record.trace_id)
            assert resolved_out == capture.outputs[record.id], record.name
    return chains


# --- a scripted chat model ---------------------------------------------------------------------------


class _ScriptedChatModel(BaseChatModel):
    """Answers turn by turn: each turn but the last carries one tool call ``{"n": turn}``."""

    turns: int = 3
    tool_name: str = "echo"
    _calls: list[int] = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedChatModel:  # type: ignore[override]
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        n = len(self._calls) + 1
        self._calls.append(n)
        calls = [] if n >= self.turns else [{"name": self.tool_name, "args": {"n": n}, "id": f"call-{n}"}]
        message = AIMessage(
            content=_answer(n),
            tool_calls=calls,
            additional_kwargs={"reasoning_content": f"thinking {n}"},
            response_metadata={"model_name": "scripted-1", "finish_reason": "tool_calls" if calls else "stop"},
            usage_metadata={"input_tokens": 10 * n, "output_tokens": n, "total_tokens": 11 * n},
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


@tool
def echo(n: int) -> str:
    """Return the n-th result marker."""
    return _result(n)


# --- a synthetic two-node graph plus a parallel pair ---------------------------------------------------


class _PairState(TypedDict):
    value: str
    seen: Annotated[list[str], operator.add]


def _synthetic_graph(writer: OtelWriter, model: BaseChatModel, current: dict[str, str | None]):
    def node(name: str):
        def body(state: _PairState, config: RunnableConfig) -> dict[str, Any]:
            current[name] = writer.current_span_id()
            if name == "a":
                model.invoke([HumanMessage(content="hello")], config)
            return {"seen": [name]}

        return body

    graph = StateGraph(_PairState)
    for name in ("a", "b", "p1", "p2"):
        graph.add_node(name, node(name))
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", "p1")
    graph.add_edge("b", "p2")
    graph.add_edge("p1", END)
    graph.add_edge("p2", END)
    return graph.compile()


def test_synthetic_graph_parentage_current_span_grouping_and_producer_mode(
    writer: OtelWriter, exporter: InMemorySpanExporter
):
    current: dict[str, str | None] = {}
    graph = _synthetic_graph(writer, _ScriptedChatModel(turns=1), current)
    config = bind_run_trace(grouping_nodes=frozenset({"a"}), chain_payloads="producer").config
    graph.invoke({"value": "v", "seen": []}, _cfg(config))
    observations = _observations(writer, exporter)
    by_name = {o.name: o for o in observations}
    root = by_name["LangGraph"]
    assert root.parent_id is None
    for name in ("a", "b", "p1", "p2"):
        assert by_name[name].parent_id == root.id, name
        assert current[name] == by_name[name].id, name
    assert by_name["a"].metadata is not None
    assert by_name["a"].metadata["tai42.step_role"] == StepRole.GROUPING.value
    for name in ("b", "p1", "p2"):
        assert "tai42.step_role" not in (by_name[name].metadata or {}), name
    for chain in (o for o in observations if o.type == SpanKind.CHAIN.value):
        assert chain.input is None, chain.name
        assert chain.output is None, chain.name
    (llm,) = [o for o in observations if o.type == SpanKind.LLM.value]
    assert llm.parent_id == by_name["a"].id
    assert llm.input == [{"role": "user", "parts": [{"type": "text", "content": "hello"}]}]
    assert llm.output[0]["parts"][0]["content"] == _answer(1)
    assert llm.output[0]["finish_reason"] == "stop"
    assert llm.usage == {"input_tokens": 10, "output_tokens": 1, "total_tokens": 11}
    assert llm.model == "scripted-1"
    assert llm.metadata is not None
    assert llm.metadata["tai42.message"]["additional_kwargs"] == {"reasoning_content": "thinking 1"}


async def test_async_parallel_nodes_each_see_their_own_span(writer: OtelWriter, exporter: InMemorySpanExporter):
    current: dict[str, str | None] = {}
    graph = _synthetic_graph(writer, _ScriptedChatModel(turns=1), current)
    await graph.ainvoke({"value": "v", "seen": []}, _cfg(bind_run_trace().config))
    by_name = {o.name: o for o in _observations(writer, exporter)}
    for name in ("a", "b", "p1", "p2"):
        assert current[name] == by_name[name].id, name


def test_interrupt_ends_the_span_without_error(writer: OtelWriter, exporter: InMemorySpanExporter):
    class S(TypedDict):
        x: int

    def ask(state: S) -> dict[str, int]:
        interrupt("need input")
        return {"x": 1}

    builder = StateGraph(S)
    builder.add_node("ask", ask)
    builder.add_edge(START, "ask")
    builder.add_edge("ask", END)
    graph = builder.compile(checkpointer=InMemorySaver())
    config = bind_run_trace({"configurable": {"thread_id": "i"}}).config
    graph.invoke({"x": 0}, _cfg(config))
    ask_span = next(o for o in _observations(writer, exporter) if o.name == "ask")
    assert ask_span.level is None
    assert ask_span.metadata is not None
    assert ask_span.metadata["interrupted"] is True


def test_chain_error_is_recorded_at_error(writer: OtelWriter, exporter: InMemorySpanExporter):
    class S(TypedDict):
        x: int

    def broken(state: S) -> dict[str, int]:
        raise RuntimeError("node broke")

    builder = StateGraph(S)
    builder.add_node("broken", broken)
    builder.add_edge(START, "broken")
    graph = builder.compile()
    with pytest.raises(RuntimeError, match="node broke"):
        graph.invoke({"x": 0}, _cfg(bind_run_trace().config))
    node = next(o for o in _observations(writer, exporter) if o.name == "broken")
    assert (node.level, node.status_message) == ("ERROR", "node broke")


# --- the agent loop shape over the default references mode ----------------------------------------------


class _LoopState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


def _agent_loop_graph(model: BaseChatModel):
    def call_model(state: _LoopState, config: RunnableConfig) -> dict[str, Any]:
        return {"messages": [model.invoke(state["messages"], config)]}

    def route(state: _LoopState) -> Any:
        last = state["messages"][-1]
        pending = last.tool_calls if isinstance(last, AIMessage) else []
        return [Send("tools", [tc]) for tc in pending] if pending else END

    builder = StateGraph(_LoopState)
    builder.add_node("model", call_model)
    builder.add_node("tools", ToolNode([echo]))
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", route, ["tools", END])
    builder.add_edge("tools", "model")
    return builder.compile()


def _markers() -> list[str]:
    return [_answer(1), _answer(2), _answer(3), _result(1), _result(2)]


def _assert_no_marker_inline_in_chains(spans: list[ReadableSpan]) -> None:
    for span in spans:
        attributes = dict(span.attributes or {})
        if attributes.get("tai42.span.kind") != SpanKind.CHAIN.value:
            continue
        text = "".join(str(v) for v in attributes.values())
        for marker in _markers():
            assert marker not in text, (span.name, marker[:10])


async def test_agent_loop_records_each_value_once_and_resolves_to_the_real_values(
    writer: OtelWriter, exporter: InMemorySpanExporter
):
    graph = _agent_loop_graph(_ScriptedChatModel(turns=3))
    config, capture = _bound_config(writer)
    await graph.ainvoke({"messages": [HumanMessage(content="start")]}, config)
    spans = _spans(writer, exporter)
    _assert_no_marker_inline_in_chains(spans)

    generations = [s for s in spans if dict(s.attributes or {}).get("tai42.span.kind") == SpanKind.LLM.value]
    tools = [s for s in spans if dict(s.attributes or {}).get("tai42.span.kind") == SpanKind.TOOL.value]
    assert len(generations) == 3
    assert len(tools) == 2
    for n, generation in enumerate(generations, start=1):
        attributes = dict(generation.attributes or {})
        assert _answer(n) in str(attributes["gen_ai.output.messages"])
        assert _answer(n) in str(attributes["tai42.metadata"])
        for earlier in range(1, n):
            assert _answer(earlier) in str(attributes["gen_ai.input.messages"])
            assert _result(earlier) in str(attributes["gen_ai.input.messages"])
    for n, tool_span in enumerate(tools, start=1):
        assert _result(n) in str(dict(tool_span.attributes or {})["gen_ai.tool.call.result"])

    observations = [span_to_observation(s) for s in spans]
    await _assert_resolved_equals_real(observations, capture, exporter)
    route_records = [o for o in observations if o.name == "route"]
    assert route_records
    first_route = route_records[0]
    assert isinstance(first_route.output, list)
    send = first_route.output[0]
    assert (send["node"], send["timeout"]) == ("tools", None)
    (ref,) = send["arg"]
    assert is_payload_ref(ref)
    assert ref["$tai42_ref"]["field"] == "metadata"
    assert ref["$tai42_ref"]["pointer"] == "/tai42.message/tool_calls/0"
    assert ref["$tai42_ref"]["span_id"] == span_to_observation(generations[0]).id


# --- the real agent factory with both in-place rebinds -------------------------------------------------------


async def test_create_agent_records_rebound_messages_member_wise(writer: OtelWriter, exporter: InMemorySpanExporter):
    agent = create_agent(_ScriptedChatModel(turns=3), tools=[echo], name="probe-agent")
    config, capture = _bound_config(writer)
    await agent.ainvoke({"messages": [HumanMessage(content="start")]}, config)
    spans = _spans(writer, exporter)
    _assert_no_marker_inline_in_chains(spans)
    observations = [span_to_observation(s) for s in spans]
    chains = await _assert_resolved_equals_real(observations, capture, exporter)
    reader = InMemoryOtelReader(exporter)

    generations = [o for o in observations if o.type == SpanKind.LLM.value]
    tools = [o for o in observations if o.type == SpanKind.TOOL.value]
    model_nodes = [o for o in chains if o.name == "model"]
    root = next(o for o in chains if o.parent_id is None)
    assert len(generations) == 3
    assert len(model_nodes) == 3
    assert {o.name for o in chains} <= {"probe-agent", "model", "tools"}

    # The generation holds the message as the model returned it: no agent name yet.
    for generation in generations:
        assert generation.metadata is not None
        assert generation.metadata["tai42.message"]["name"] is None
    first_out = model_nodes[0].output
    assert isinstance(first_out, list)
    recorded = first_out[0]["update"]["messages"][0]
    assert recorded["name"] == "probe-agent"
    for key in ("content", "additional_kwargs", "response_metadata", "tool_calls"):
        assert is_payload_ref(recorded[key]), key
        assert recorded[key]["$tai42_ref"]["span_id"] == generations[0].id
    assert model_nodes[0].trace_id is not None
    resolved_out = await resolve_refs(first_out, reader, trace_id=model_nodes[0].trace_id)
    assert resolved_out[0]["update"]["messages"][0]["name"] == "probe-agent"

    # The tool result is id-less at its tool record; the tools node's record (written after the id was
    # assigned) holds it member-wise with the id inline and the content a reference into the tool record,
    # and the next model input carries it as ONE reference to that member-wise form.
    assert tools[0].output["id"] is None
    tools_node = next(o for o in chains if o.name == "tools")
    tool_message = tools_node.output["messages"][0]
    uuid.UUID(tool_message["id"])
    assert is_payload_ref(tool_message["content"])
    assert tool_message["content"]["$tai42_ref"]["span_id"] == tools[0].id
    second_in = model_nodes[1].input["messages"]
    assert is_payload_ref(second_in[2])
    assert second_in[2]["$tai42_ref"] == {"span_id": tools_node.id, "field": "output", "pointer": "/messages/0"}

    # The caller's message: inline and id-less at the root, member-wise with its uuid at the first model input.
    root_human = root.input["messages"][0]
    assert root_human["content"] == "start"
    assert root_human["id"] is None
    first_in_human = model_nodes[0].input["messages"][0]
    assert isinstance(first_in_human["id"], str)
    uuid.UUID(first_in_human["id"])

    # Every later chain record carries each of those messages as ONE reference.
    for later in model_nodes[1:]:
        assert all(is_payload_ref(m) for m in later.input["messages"]), later.name
    assert all(is_payload_ref(m) for m in root.output["messages"])


class _Counter(TypedDict):
    x: int


def _inc(state: _Counter) -> dict[str, int]:
    return {"x": state["x"] + 1}


# --- declared payloads ------------------------------------------------------------------------------------------


def test_declared_chain_payload_records_the_build_on_the_declared_run_only(
    writer: OtelWriter, exporter: InMemorySpanExporter
):
    seen_current: list[str | None] = []

    def build(value: Any) -> Any:
        seen_current.append(writer.current_span_id())
        return {"built": sorted(value)}

    builder = StateGraph(_Counter)
    builder.add_node("inc", _inc)
    builder.add_edge(START, "inc")
    graph = builder.compile()
    run_id = uuid.uuid4()
    config = bind_run_trace(chain_payloads="producer").config
    with declared_chain_payload(run_id, build):
        graph.invoke({"x": 1}, _cfg({**config, "run_id": run_id}))
    observations = _observations(writer, exporter)
    root = next(o for o in observations if o.parent_id is None)
    assert root.input == {"built": ["x"]}
    assert root.output == {"built": ["x"]}
    assert len(seen_current) == 2
    assert seen_current[0] == root.id
    for other in observations:
        if other.id != root.id:
            assert other.input is None, other.name


def test_a_raising_build_records_the_failure_visibly(writer: OtelWriter, exporter: InMemorySpanExporter):
    def build(value: Any) -> Any:
        raise ValueError("bad build")

    builder = StateGraph(_Counter)
    builder.add_node("inc", _inc)
    builder.add_edge(START, "inc")
    graph = builder.compile()
    run_id = uuid.uuid4()
    with declared_chain_payload(run_id, build):
        result = graph.invoke({"x": 1}, _cfg({**bind_run_trace().config, "run_id": run_id}))
    assert result == {"x": 2}
    root = next(o for o in _observations(writer, exporter) if o.parent_id is None)
    assert root.input == {"error_type": "ValueError", "message": "bad build"}


# --- the shallow snapshot ---------------------------------------------------------------------------------------------


class _Model(BaseModel):
    a: str = "x"
    nested: dict[str, Any] = {}


def test_shallow_members_detect_a_rebound_top_level_field_only():
    nested = {"k": 1}
    model = _Model(nested=nested)
    before = _shallow_members(model)
    model.nested["k"] = 2
    assert _shallow_members(model) == before
    assert all(c[1] is s[1] for c, s in zip(_shallow_members(model), before, strict=True))
    model.a = "y"
    assert _shallow_members(model) != before


def test_shallow_members_of_a_dict_see_an_added_key():
    value = {"a": 1}
    before = _shallow_members(value)
    value["b"] = 2
    assert _shallow_members(value) != before


def test_a_str_is_never_stale():
    assert _shallow_members("text") == ()


def test_a_dict_with_an_added_key_is_written_member_wise(writer: OtelWriter, exporter: InMemorySpanExporter):
    handler = MonitoringCallbackHandler(writer, bind_run_trace().context)
    origin = {"big": "z" * 300, "small": 1}
    handler._register(origin, "a" * 16, "input", "/v")
    origin["added"] = [1, 2]
    form = handler._manifest(origin, "b" * 16, "output", "", 0)
    assert form["small"] == 1
    assert form["added"] == [1, 2]
    assert form["big"].ref.pointer == "/v/big"
    assert handler._manifest(origin, "c" * 16, "output", "", 0).ref.span_id == "b" * 16


def test_messages_round_trip_through_the_message_shape():
    from tai42_kit.llm.monitoring_callbacks import gen_ai_message

    assert gen_ai_message(ToolMessage(content="r", tool_call_id="c")) == {
        "role": "tool",
        "parts": [{"type": "tool_call_response", "id": "c", "response": "r"}],
    }
    ai = AIMessage(content=[{"type": "text", "text": "t"}, {"type": "image", "url": "u"}, "s"])
    assert gen_ai_message(ai)["parts"] == [
        {"type": "text", "content": "t"},
        {"type": "image", "url": "u"},
        {"type": "text", "content": "s"},
    ]
