"""Every agent records its run as ONE trace rooted at ONE span, through the kit's run-trace opener.

One drive per agent face runs the agent's real code against the recording monitoring backend
bound in ``conftest.py`` (the kit's real OpenTelemetry writer over an in-memory exporter,
recorded through the kit's monitoring callback handler) and a scripted real chat model.

* Driven with no live span and no ambient deposit, a run writes exactly one span with no parent,
  named ``run`` (claude_code: its one usage span), and every other span of the trace sits under it;
  the model calls are among them.
* That ``run`` span records the run's input (the turn it was given) and its output (a block run's
  result, a raw chunk stream's last chunk, an event stream's terminal events).
* Driven under an ambient deposit, no ``run`` span opens: the run's top spans are children of the
  deposit's span, in its trace.
* Driven under a live span with no deposit, one ``run`` span opens as that span's child and every
  other span of the run sits under it.
* Every stream drive is also abandoned after its first item (dropped, collected, acloseed by the
  loop's async-generator finalizer) and acloseed from another task after its first item: the
  consumer's deposit and current span never change across an item, nothing raises, the loop's
  exception handler receives nothing, and the ``run`` span ends with no error level.
* Driven inside a run attribution, every span the run writes carries it.

The tools-agent reference case drives two tool turns and proves every chain record of the
``create_agent`` graph resolves, through its references, to exactly the value the chain
callback received, with no message content repeated inline after its first record.
"""

from __future__ import annotations

import asyncio
import gc
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, cast

import orjson
import pytest
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult
from langgraph.checkpoint.memory import InMemorySaver
from opentelemetry.sdk.trace import ReadableSpan
from pydantic import PrivateAttr, SecretStr
from tai42_contract.app import tai42_app
from tai42_contract.monitoring import (
    RUN_ATTRIBUTION_TRACE_NAME,
    MonitoringLevel,
    RunAttribution,
    SpanKind,
    TraceContext,
    ambient_trace_context,
    attribute_run,
    get_ambient_trace_context,
)
from tai42_contract.template import TemplatedText
from tai42_kit.monitoring import encode_payload, resolve_refs
from tai42_kit.monitoring.otel import attributes as attr
from tai42_kit.monitoring.testing import span_to_observation

import tai42_agents.claude_code.agent as claude_code_mod
import tai42_agents.claude_code.workspace as claude_workspace_mod
from tai42_agents._internal import base_tool_agent as bta
from tai42_agents._internal.stream_events import astream_tools_agent_events
from tai42_agents.claude_code.agent import ClaudeCodeAgent
from tai42_agents.claude_code.settings import ClaudeCodeSettings
from tai42_agents.langchain_deep_agent.agent import DeepAgent
from tai42_agents.refine_agent import agent as refine_mod
from tai42_agents.refine_agent.agent import RefineAgent
from tai42_agents.refine_agent.prompt import CRITIC_APPROVAL_MESSAGE
from tai42_agents.retrieval_tools_agent import agent as ragent
from tai42_agents.retrieval_tools_agent.agent import RetrievalToolsAgent
from tai42_agents.voting_agent import agent as voting_mod
from tai42_agents.voting_agent.agent import VotingAgent
from tai42_agents.voting_agent.model import VoterSpec
from tai42_agents.vqa_agent import VqaAgent

from ._claude_app import build_local_app
from ._claude_stubs import MESSAGE, payload_for
from ._client_tool import client_tool
from ._deep_agent_fakes import _FakeCompiledGraph, _install_fake_resolve
from ._graph_support import compiled_graph, invoke_tools_agent
from ._retrieval_tools_agent_support import StubStore
from .conftest import RecordingMonitoringWriter


class _RecordingChatModel(BaseChatModel):
    """A real chat model that replays scripted messages and fires ``on_chat_model_start``.

    Being a genuine ``BaseChatModel``, the LangChain callback machinery fires
    ``on_chat_model_start`` on the monitoring handlers riding the run config — the behavior
    the per-invoke seam exists to deliver. ``bind_tools`` is a no-op so an agent can bind
    its tools and still be scripted. ``profile`` opts the model into the kit's native
    structured-output path when a case needs it.
    """

    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(self, responses: Sequence[BaseMessage], profile: dict[str, Any] | None = None, **kwargs: Any) -> None:
        # ``profile`` is the base ``BaseChatModel`` capability field the kit's structured
        # plan reads to choose native vs tool structured output; a dict stands in for it.
        super().__init__(profile=cast(Any, profile), **kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "recording"

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        message = self._responses[min(self._index, len(self._responses) - 1)]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        return self


def _writer() -> RecordingMonitoringWriter:
    return cast(RecordingMonitoringWriter, tai42_app.monitoring.active.writer)


@pytest.fixture(autouse=True)
def _reset_recording() -> None:
    _writer().clear()


def _kind(span: ReadableSpan) -> object:
    return (span.attributes or {}).get(attr.SPAN_KIND)


def _trace_id(span: ReadableSpan) -> str:
    assert span.context is not None
    return format(span.context.trace_id, "032x")


def _span_id(span: ReadableSpan) -> str:
    assert span.context is not None
    return format(span.context.span_id, "016x")


def _parent_id(span: ReadableSpan) -> str | None:
    return format(span.parent.span_id, "016x") if span.parent is not None else None


def _named(spans: list[ReadableSpan], name: str) -> list[ReadableSpan]:
    return [s for s in spans if s.name == name]


def _descends_from(span: ReadableSpan, ancestor: ReadableSpan, spans: list[ReadableSpan]) -> bool:
    by_id = {_span_id(s): s for s in spans}
    parent = _parent_id(span)
    while parent is not None:
        if parent == _span_id(ancestor):
            return True
        if parent not in by_id:
            return False
        parent = _parent_id(by_id[parent])
    return False


def _assert_not_failed(span: ReadableSpan) -> None:
    assert (span.attributes or {}).get(attr.LEVEL) != MonitoringLevel.ERROR.value, (span.name, span.attributes)


async def _empty_middlewares() -> list[Any]:
    return []


async def _fake_embed(_text: str) -> list[float]:
    return [0.0] * 6


_STRUCTURED_SCHEMA = {"title": "Answer", "type": "object", "properties": {"answer": {"type": "string"}}}
_DEPOSIT = TraceContext(trace_id="d" * 32, parent_span_id="e" * 16)
_ATTRIBUTION = RunAttribution(tags=["alpha", "beta"], user_id="alice", session_id="thread-1")
_CLAUDE_DIGEST = "registry.example/claude@sha256:" + "c" * 64


# --- the drives ------------------------------------------------------------------------


@dataclass(frozen=True)
class _Drive:
    """One agent face: ``prepare`` patches its seams and returns the opener of one run.

    A stream drive's opener returns the agent's own async generator; a block drive's opener
    returns the run's awaited result. ``run_input`` is the input the run's root records.
    ``raw_chunks`` marks a stream of raw graph chunks rather than platform events.
    ``min_model_calls`` is the model calls one run records.
    """

    name: str
    prepare: Callable[[pytest.MonkeyPatch], Callable[[], Awaitable[Any]]]
    stream: bool
    run_input: dict[str, Any]
    raw_chunks: bool = False
    min_model_calls: int = 1


def _patch_tools_seams(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel) -> None:
    saver = InMemorySaver()
    monkeypatch.setattr(
        bta,
        "llm_provider_settings",
        lambda: SimpleNamespace(llm="anthropic", checkpoint="c", checkpoint_conn_string=None),
    )
    monkeypatch.setattr(bta, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))
    monkeypatch.setattr(bta, "context_overflow_middlewares", lambda **_: _empty_middlewares())

    async def fake_get_llm(*, provider: str, **_kwargs: Any) -> BaseChatModel:
        return model

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    monkeypatch.setattr(bta, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(bta, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))


def _tools_invoke(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    _patch_tools_seams(monkeypatch, _RecordingChatModel([AIMessage(content="done")]))
    return lambda: invoke_tools_agent(system_message="sys", user_message=["hi"], tools=[])


def _tools_stream(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    _patch_tools_seams(monkeypatch, _RecordingChatModel([AIMessage(content="done")]))

    async def open_stream() -> AsyncIterator[Any]:
        graph = await compiled_graph("sys", [], None, None, None, None, None)
        return bta.astream_tools_agent(graph, ["hi"], None, "values", None)

    return open_stream


def _tools_events(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    _patch_tools_seams(monkeypatch, _RecordingChatModel([AIMessage(content="done")]))

    async def open_stream() -> AsyncIterator[Any]:
        graph = await compiled_graph("sys", [], None, None, None, None, None)
        return astream_tools_agent_events(graph, ["hi"])

    return open_stream


def _voting(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    # One voter plus the judge each drive a tools-agent graph under the one run.
    _patch_tools_seams(monkeypatch, _RecordingChatModel([AIMessage(content="verdict")]))
    monkeypatch.setattr(voting_mod, "llm_provider_settings", lambda: SimpleNamespace(llm="anthropic"))

    async def open_stream() -> AsyncIterator[Any]:
        return VotingAgent().astream(
            judge_message=TemplatedText(content="decide"),
            voter_message=TemplatedText(content="vote"),
            voters=[VoterSpec(provider="anthropic")],
        )

    return open_stream


def _refine(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    # The evaluator drafts, the critic approves on the first pass, the final pass re-runs the evaluator.
    model = _RecordingChatModel(
        [AIMessage(content="draft"), AIMessage(content=CRITIC_APPROVAL_MESSAGE), AIMessage(content="final")]
    )
    saver = InMemorySaver()
    monkeypatch.setattr(
        refine_mod,
        "llm_provider_settings",
        lambda: SimpleNamespace(llm="anthropic", checkpoint="c", checkpoint_conn_string=None),
    )
    monkeypatch.setattr(refine_mod, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))
    monkeypatch.setattr(refine_mod, "logging_settings", lambda: SimpleNamespace(is_enabled_for=lambda level: False))
    monkeypatch.setattr(refine_mod, "context_overflow_middlewares", lambda **_: _empty_middlewares())

    async def fake_get_llm(*, provider: str, **_kwargs: Any) -> BaseChatModel:
        return model

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    monkeypatch.setattr(refine_mod, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(
        refine_mod, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer)
    )

    async def open_stream() -> AsyncIterator[Any]:
        return RefineAgent().astream(
            evaluator_message=TemplatedText(content="write"),
            critic_message=TemplatedText(content="review"),
            max_iterations=3,
        )

    return open_stream


def _retrieval(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    # The graph's model turn emits the terminal status envelope; the structured finalization
    # (a direct model call) forces the schema.
    model = _RecordingChatModel(
        [
            AIMessage(content='{"status": "success", "message": "m", "result": "the answer"}'),
            AIMessage(content='{"answer": "the answer"}'),
        ],
        profile={"structured_output": True},
    )
    saver = InMemorySaver()
    provider_settings = SimpleNamespace(
        llm="openai",
        embedding="emb",
        checkpoint="ckpt",
        store="store",
        store_conn_string=None,
        checkpoint_conn_string=None,
    )
    monkeypatch.setattr(ragent, "llm_provider_settings", lambda: provider_settings)
    monkeypatch.setattr(ragent, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs)))
    monkeypatch.setattr(
        ragent, "embedding_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: dict(kwargs))
    )

    async def fake_resolve_tools(app_tools: Any, names: list[str], tools: list[Any], presets: list[Any]) -> list[Any]:
        return []

    async def fake_get_llm(*, provider: str, **_kwargs: Any) -> BaseChatModel:
        return model

    embedding = SimpleNamespace(aembed_query=_fake_embed)

    async def fake_get_embedding(*, provider: str, **_kwargs: Any) -> Any:
        return embedding

    async def fake_get_store(*, provider: str, conn_string: Any, **_kwargs: Any) -> Any:
        return StubStore([])

    async def fake_get_checkpointer(*, provider: str, conn_string: Any) -> Any:
        return saver

    monkeypatch.setattr(ragent, "resolve_tools", fake_resolve_tools)
    monkeypatch.setattr(ragent, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(ragent, "get_embedding_async", fake_get_embedding)
    monkeypatch.setattr(ragent, "store_registry", lambda: SimpleNamespace(get_store=fake_get_store))
    monkeypatch.setattr(ragent, "checkpoint_registry", lambda: SimpleNamespace(get_checkpointer=fake_get_checkpointer))

    async def open_stream() -> AsyncIterator[Any]:
        return RetrievalToolsAgent().astream(
            user_message=TemplatedText(content="hi"), response_format=_STRUCTURED_SCHEMA
        )

    return open_stream


def _patch_vqa(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, provider: str) -> None:
    from tai42_agents import vqa_agent as vqa

    async def fake_get_llm(provider: str, **_kwargs: Any) -> BaseChatModel:
        return model

    monkeypatch.setattr(vqa, "get_llm_async", fake_get_llm)
    monkeypatch.setattr(vqa, "llm_settings", lambda: SimpleNamespace(with_fallbacks=lambda kwargs: {}))
    monkeypatch.setattr(vqa, "llm_provider_settings", lambda: SimpleNamespace(llm=provider))


def _vqa_text(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    _patch_vqa(monkeypatch, _RecordingChatModel([AIMessage(content="a cat")]), provider="p")

    async def open_stream() -> AsyncIterator[Any]:
        return VqaAgent().astream(image_url="http://img", query="describe")

    return open_stream


def _vqa_structured(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    # The first answer misses the schema, so the structured door re-prompts once.
    model = _RecordingChatModel(
        [AIMessage(content="not json"), AIMessage(content='{"answer": "a cat"}')], profile={"structured_output": True}
    )
    _patch_vqa(monkeypatch, model, provider="openai")

    async def open_stream() -> AsyncIterator[Any]:
        return VqaAgent().astream(image_url="http://img", query="describe", response_format=_STRUCTURED_SCHEMA)

    return open_stream


class _CallbackFiringGraph(_FakeCompiledGraph):
    """A scripted compiled graph that fires ``on_chat_model_start`` from the run config.

    A real deepagents graph propagates the run config's callbacks to its model node (the
    langgraph ``var_child_runnable_config`` discipline). This stand-in fires the handlers
    the config carries, so the drive proves the deep agent handed the per-invoke monitoring
    config to the graph its model node runs under.
    """

    async def astream(self, agent_input: Any, config: Any, stream_mode: Any = None) -> Any:
        self.received_input = agent_input
        self.received_config = config
        for handler in (config or {}).get("callbacks", []):
            if hasattr(handler, "on_chat_model_start"):
                run_id = uuid.uuid4()
                handler.on_chat_model_start({}, [[]], run_id=run_id)
                handler.on_llm_end(LLMResult(generations=[[]]), run_id=run_id)
        for chunk in self._chunks:
            yield chunk


def _deep(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    # Only the graph build is faked, so the real run-config seam hands the graph the run's callbacks.
    agent = DeepAgent()
    graph = _CallbackFiringGraph(
        [
            ("updates", {"agent": {"messages": [AIMessage(content="step")]}}),
            ("updates", {"agent": {"messages": [AIMessage(content="done")]}}),
        ],
        interrupts=[],
    )
    _install_fake_resolve(monkeypatch, agent, graph)

    async def open_stream() -> AsyncIterator[Any]:
        return agent.astream(user_message=TemplatedText(content="go"), thread_id="t")

    return open_stream


def _deep_run(monkeypatch: pytest.MonkeyPatch) -> Callable[[], Awaitable[Any]]:
    # The block face drains the same streaming core inside one held run root.
    agent = DeepAgent()
    graph = _CallbackFiringGraph(
        [("updates", {"agent": {"messages": [AIMessage(content="done")]}})],
        interrupts=[],
    )
    _install_fake_resolve(monkeypatch, agent, graph)
    return lambda: agent.run(user_message=TemplatedText(content="go"), thread_id="t")


_HI = {"user_message": ["hi"]}
_DRIVES = [
    _Drive("tools_agent.ainvoke", _tools_invoke, stream=False, run_input=_HI),
    _Drive("tools_agent.astream", _tools_stream, stream=True, run_input=_HI, raw_chunks=True),
    _Drive("tools_agent.astream_events", _tools_events, stream=True, run_input=_HI),
    _Drive(
        "retrieval_tools_agent",
        _retrieval,
        stream=True,
        run_input={"user_message": TemplatedText(content="hi")},
        min_model_calls=2,
    ),
    _Drive("langchain_deep_agent", _deep, stream=True, run_input={"user_message": TemplatedText(content="go")}),
    _Drive(
        "langchain_deep_agent.run", _deep_run, stream=False, run_input={"user_message": TemplatedText(content="go")}
    ),
    _Drive(
        "refine_agent",
        _refine,
        stream=True,
        run_input={
            "evaluator_message": TemplatedText(content="write"),
            "critic_message": TemplatedText(content="review"),
        },
        min_model_calls=3,
    ),
    _Drive(
        "voting_agent",
        _voting,
        stream=True,
        run_input={"judge_message": TemplatedText(content="decide"), "voter_message": TemplatedText(content="vote")},
        min_model_calls=2,
    ),
    _Drive("vqa_agent.text", _vqa_text, stream=True, run_input={"image_url": "http://img", "query": "describe"}),
    _Drive(
        "vqa_agent.structured",
        _vqa_structured,
        stream=True,
        run_input={"image_url": "http://img", "query": "describe"},
        min_model_calls=2,
    ),
]
_STREAM_DRIVES = [drive for drive in _DRIVES if drive.stream]


def _consumer_state() -> tuple[TraceContext | None, str | None]:
    return get_ambient_trace_context(), _writer().current_span_id()


async def _drain(drive: _Drive, opener: Callable[[], Awaitable[Any]], *, items_keep_the_consumer_state: bool) -> None:
    """Run ``opener`` to its end; a held run root leaves the consumer's deposit and current span after every item."""
    if not drive.stream:
        await opener()
        return
    before = _consumer_state()
    stream = await opener()
    async for _ in stream:
        if items_keep_the_consumer_state:
            assert _consumer_state() == before


def _run_drive(
    drive: _Drive,
    monkeypatch: pytest.MonkeyPatch,
    *,
    wrap: Callable[[], Any] | None = None,
    items_keep_the_consumer_state: bool = True,
) -> None:
    opener = drive.prepare(monkeypatch)

    async def main() -> None:
        if wrap is None:
            await _drain(drive, opener, items_keep_the_consumer_state=items_keep_the_consumer_state)
            return
        with wrap():
            await _drain(drive, opener, items_keep_the_consumer_state=items_keep_the_consumer_state)

    asyncio.run(main())


# --- one run, one root ------------------------------------------------------------------


@pytest.mark.parametrize("drive", _DRIVES, ids=lambda d: d.name)
def test_a_run_with_no_lineage_writes_one_run_root(drive: _Drive, monkeypatch: pytest.MonkeyPatch) -> None:
    _run_drive(drive, monkeypatch)

    spans = _writer().spans()
    roots = [s for s in spans if s.parent is None]
    assert [s.name for s in roots] == [RUN_ATTRIBUTION_TRACE_NAME]
    (root,) = roots
    _assert_not_failed(root)
    assert {_trace_id(s) for s in spans} == {_trace_id(root)}
    assert all(_descends_from(s, root, spans) for s in spans if s is not root)
    assert len([s for s in spans if _kind(s) == SpanKind.LLM.value]) >= drive.min_model_calls


def _encoded(value: Any) -> Any:
    return orjson.loads(encode_payload(value))


async def _run_output(drive: _Drive, opener: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``opener`` to its end and return what the run produced, as its root records it.

    A block run's result; a raw chunk stream's last chunk; an event stream's terminal events.
    """
    if not drive.stream:
        return await opener()
    items = [item async for item in await opener()]
    if drive.raw_chunks:
        return items[-1]
    return [item for item in items if item.final]


@pytest.mark.parametrize("drive", _DRIVES, ids=lambda d: d.name)
def test_a_run_root_records_the_run_input_and_its_output(drive: _Drive, monkeypatch: pytest.MonkeyPatch) -> None:
    opener = drive.prepare(monkeypatch)
    output = asyncio.run(_run_output(drive, opener))

    (root,) = [s for s in _writer().spans() if s.parent is None]
    record = span_to_observation(root)
    assert record.input == _encoded(drive.run_input)
    assert output
    assert record.output == _encoded(output)


@pytest.mark.parametrize("drive", _DRIVES, ids=lambda d: d.name)
def test_a_run_under_an_ambient_deposit_opens_no_run_span(drive: _Drive, monkeypatch: pytest.MonkeyPatch) -> None:
    # A kept lineage holds no run root, so nothing scopes a span the graph's callbacks make current.
    _run_drive(drive, monkeypatch, wrap=lambda: ambient_trace_context(_DEPOSIT), items_keep_the_consumer_state=False)

    spans = _writer().spans()
    assert spans
    assert not _named(spans, RUN_ATTRIBUTION_TRACE_NAME)
    assert {_trace_id(s) for s in spans} == {_DEPOSIT.trace_id}
    ids = {_span_id(s) for s in spans}
    tops = [s for s in spans if _parent_id(s) not in ids]
    assert tops
    assert {_parent_id(s) for s in tops} == {_DEPOSIT.parent_span_id}


@contextmanager
def _live_span() -> Iterator[None]:
    writer = _writer()
    outer = writer.open_span(name="outer", kind=SpanKind.CHAIN)
    try:
        with writer.use_span(outer):
            yield
    finally:
        outer.end()


@pytest.mark.parametrize("drive", _DRIVES, ids=lambda d: d.name)
def test_a_run_under_a_live_span_opens_its_run_span_as_its_child(
    drive: _Drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_drive(drive, monkeypatch, wrap=_live_span)

    spans = _writer().spans()
    (outer,) = _named(spans, "outer")
    (run,) = _named(spans, RUN_ATTRIBUTION_TRACE_NAME)
    assert _parent_id(run) == _span_id(outer)
    _assert_not_failed(run)
    assert all(_descends_from(s, run, spans) for s in spans if s is not outer and s is not run)


@pytest.mark.parametrize("drive", _DRIVES, ids=lambda d: d.name)
def test_every_span_of_an_attributed_run_carries_the_attribution(
    drive: _Drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run_drive(drive, monkeypatch, wrap=lambda: attribute_run(_writer(), _ATTRIBUTION))

    spans = _writer().spans()
    assert _named(spans, RUN_ATTRIBUTION_TRACE_NAME)
    for span in spans:
        _assert_attributed(span)


def _assert_attributed(span: ReadableSpan) -> None:
    attributes = dict(span.attributes or {})
    assert attributes.get(attr.TRACE_NAME) == RUN_ATTRIBUTION_TRACE_NAME, (span.name, attributes)
    assert {"alpha", "beta"} <= set(orjson.loads(str(attributes.get(attr.TRACE_TAGS, "[]")))), (span.name, attributes)
    assert attributes.get(attr.USER_ID) == "alice", (span.name, attributes)
    assert attributes.get(attr.SESSION_ID) == "thread-1", (span.name, attributes)


# --- a stream dropped or closed part-way ------------------------------------------------


def _assert_the_run_span_ended_clean() -> None:
    (run,) = _named(_writer().spans(), RUN_ATTRIBUTION_TRACE_NAME)
    _assert_not_failed(run)


async def _settle() -> None:
    for _ in range(100):
        await asyncio.sleep(0)


def _run_leg(leg: Callable[[], Awaitable[None]]) -> list[dict[str, Any]]:
    """Run ``leg`` on a fresh loop; return what the loop's exception handler received."""
    received: list[dict[str, Any]] = []

    async def main() -> None:
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: received.append(context))
        await leg()

    asyncio.run(main())
    return received


@pytest.mark.parametrize("drive", _STREAM_DRIVES, ids=lambda d: d.name)
def test_a_stream_abandoned_after_its_first_item_ends_its_run_cleanly(
    drive: _Drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = drive.prepare(monkeypatch)

    async def leg() -> None:
        before = _consumer_state()
        stream = await opener()
        async for _ in stream:
            assert _consumer_state() == before
            break
        del stream
        gc.collect()
        await _settle()
        assert _consumer_state() == before

    assert _run_leg(leg) == []
    _assert_the_run_span_ended_clean()


@pytest.mark.parametrize("drive", _STREAM_DRIVES, ids=lambda d: d.name)
def test_a_stream_closed_from_another_task_after_its_first_item_ends_its_run_cleanly(
    drive: _Drive, monkeypatch: pytest.MonkeyPatch
) -> None:
    opener = drive.prepare(monkeypatch)

    async def leg() -> None:
        before = _consumer_state()
        stream = await opener()
        async for _ in stream:
            assert _consumer_state() == before
            break
        await asyncio.create_task(stream.aclose())
        assert _consumer_state() == before

    assert _run_leg(leg) == []
    _assert_the_run_span_ended_clean()


# --- claude_code: its one usage span ---------------------------------------------------


def _claude_code(monkeypatch: pytest.MonkeyPatch, *, wrap: Callable[[], Any] | None = None) -> list[ReadableSpan]:
    settings = ClaudeCodeSettings(session_image=_CLAUDE_DIGEST, api_key=SecretStr("modelkey"), model="claude-x")
    monkeypatch.setattr(claude_code_mod, "claude_code_settings", lambda: settings)
    monkeypatch.setattr(claude_workspace_mod, "runner_payload_files", payload_for(MESSAGE))
    writer = _writer()

    async def main() -> None:
        with tai42_app.bound(build_local_app(writer=writer)):
            if wrap is None:
                _ = [event async for event in ClaudeCodeAgent().astream(user_message=TemplatedText(content="hi"))]
                return
            with wrap():
                _ = [event async for event in ClaudeCodeAgent().astream(user_message=TemplatedText(content="hi"))]

    asyncio.run(main())
    return writer.spans()


def test_a_claude_code_run_with_no_lineage_roots_its_usage_span(monkeypatch: pytest.MonkeyPatch) -> None:
    (usage,) = _claude_code(monkeypatch)
    assert usage.name == "claude_code.generation"
    assert usage.parent is None


def test_a_claude_code_run_under_an_ambient_deposit_nests_its_usage_span_under_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (usage,) = _claude_code(monkeypatch, wrap=lambda: ambient_trace_context(_DEPOSIT))
    assert _trace_id(usage) == _DEPOSIT.trace_id
    assert _parent_id(usage) == _DEPOSIT.parent_span_id


def test_a_claude_code_run_under_a_live_span_opens_its_usage_span_as_its_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spans = _claude_code(monkeypatch, wrap=_live_span)
    (outer,) = _named(spans, "outer")
    (usage,) = _named(spans, "claude_code.generation")
    assert _parent_id(usage) == _span_id(outer)
    assert len(spans) == 2


def test_a_claude_code_usage_span_carries_the_run_attribution(monkeypatch: pytest.MonkeyPatch) -> None:
    (usage,) = _claude_code(monkeypatch, wrap=lambda: attribute_run(_writer(), _ATTRIBUTION))
    _assert_attributed(usage)


# --- the reference tools agent: each value recorded once, every chain record resolvable ------------------


def _marker(kind: str, n: int) -> str:
    return f"{kind}-{n}-" + "m" * 4096


def _sends_as_dicts(value: Any) -> Any:
    from dataclasses import fields, is_dataclass

    from langgraph.types import Send

    if isinstance(value, Send):
        return {"node": value.node, "arg": _sends_as_dicts(value.arg), "timeout": None}
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _sends_as_dicts(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {k: _sends_as_dicts(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sends_as_dicts(v) for v in value]
    return value


class _CaptureChainValues(BaseCallbackHandler):
    """Registered after the monitoring handler: each chain run's real input/output, keyed by its record id."""

    run_inline = True

    def __init__(self) -> None:
        self.span_of: dict[Any, str] = {}
        self.inputs: dict[str, Any] = {}
        self.outputs: dict[str, Any] = {}

    def on_chain_start(self, serialized: Any, inputs: Any, *, run_id: Any, **kwargs: Any) -> None:
        span_id = _writer().current_span_id()
        assert span_id is not None
        self.span_of[run_id] = span_id
        self.inputs[span_id] = orjson.loads(encode_payload(_sends_as_dicts(inputs)))

    def on_chain_end(self, outputs: Any, *, run_id: Any, **kwargs: Any) -> None:
        self.outputs[self.span_of[run_id]] = orjson.loads(encode_payload(_sends_as_dicts(outputs)))


def test_tools_agent_records_each_message_once_and_every_chain_record_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = [
        AIMessage(content=_marker("ANSWER", 1), tool_calls=[{"name": "echo", "args": {"n": 1}, "id": "c1"}]),
        AIMessage(content=_marker("ANSWER", 2), tool_calls=[{"name": "echo", "args": {"n": 2}, "id": "c2"}]),
        AIMessage(content=_marker("ANSWER", 3)),
    ]
    _patch_tools_seams(monkeypatch, _RecordingChatModel(calls))
    capture = _CaptureChainValues()
    real_init = bta.init_langgraph_config

    def init_with_capture(config: dict[str, Any] | None, context: TraceContext | None) -> dict[str, Any]:
        bound = real_init(config, context)
        bound["callbacks"] = [*bound["callbacks"], capture]
        return bound

    monkeypatch.setattr(bta, "init_langgraph_config", init_with_capture)

    def echo(n: int) -> str:
        return _marker("RESULT", n)

    tool = client_tool("echo", echo, description="Return the n-th result marker.")
    asyncio.run(invoke_tools_agent(system_message="sys", user_message=["start"], tools=[tool]))

    spans = _writer().spans()
    markers = [_marker("ANSWER", n) for n in (1, 2, 3)] + [_marker("RESULT", n) for n in (1, 2)]
    chains = [s for s in spans if _kind(s) == SpanKind.CHAIN.value and s.name != RUN_ATTRIBUTION_TRACE_NAME]
    assert {s.name for s in chains} >= {"model", "tools"}
    for span in chains:
        text = "".join(str(v) for v in (span.attributes or {}).values())
        for marker in markers:
            assert marker not in text, (span.name, marker[:9])

    async def resolve_all() -> None:
        reader = _writer().reader
        for span in chains:
            record = span_to_observation(span)
            assert record.trace_id is not None
            assert await resolve_refs(record.input, reader, trace_id=record.trace_id) == capture.inputs[record.id]
            assert await resolve_refs(record.output, reader, trace_id=record.trace_id) == capture.outputs[record.id]

    asyncio.run(resolve_all())
