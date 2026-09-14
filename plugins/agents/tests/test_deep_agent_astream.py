"""``DeepAgent.astream`` projection — every contract event kind, the interrupt path,
structured-final parity with ``run``, and dead-chain detachment.

A scripted compiled graph (:mod:`tests._deep_agent_fakes`) drives every event kind
with no live LLM or real store. Async code is driven with ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage, AIMessageChunk
from tai42_contract.agent.events import (
    InterruptFinal,
    MessageDelta,
    MessageFinal,
    ReasoningStep,
    RunUsage,
    StructuredFinal,
    ToolCallStep,
    ToolResultStep,
)
from tai42_contract.interactions import (
    chained_park_context,
    new_chained_park_key,
    reset_park_completion,
    reset_resume_continuation_tool,
    resolve_park_adoption,
    set_park_completion,
    set_resume_continuation_tool,
)
from tai42_contract.template import TemplatedText
from tai42_kit.utils.data.json_schema_util import JsonSchemaValidationError
from tests._deep_agent_fakes import (
    _client_tool,
    _drain_astream,
    _FakeCompiledGraph,
    _install_fake_graph,
    _install_fake_resolve,
    _scripted_chunks,
)

from tai42_agents._internal.park import AGENT_RESUME_TOOL_NAME, CHAINED_PARK_DELIVERY_TOOL_NAME
from tai42_agents._internal.park import index as idx
from tai42_agents._internal.park.index import is_resolved_tombstone, read_park_entry
from tai42_agents.langchain_deep_agent.agent import DeepAgent


def test_astream_emits_every_event_kind_and_interrupt(monkeypatch: pytest.MonkeyPatch) -> None:
    agent = DeepAgent()
    graph = _FakeCompiledGraph(
        _scripted_chunks(),
        interrupts=[SimpleNamespace(id="i-1", value={"q": "pick"})],
    )
    _install_fake_graph(monkeypatch, agent, graph)

    async def collect() -> list[Any]:
        return [
            event
            async for event in agent.astream(
                user_message=TemplatedText(content="go"), interrupt_on={"task": True}, thread_id="t"
            )
        ]

    events = asyncio.run(collect())
    by_type = {type(e) for e in events}
    assert ReasoningStep in by_type
    assert ToolCallStep in by_type
    assert ToolResultStep in by_type
    assert MessageDelta in by_type
    assert MessageFinal in by_type
    assert RunUsage in by_type
    assert StructuredFinal in by_type
    assert InterruptFinal in by_type

    reasoning = next(e for e in events if isinstance(e, ReasoningStep))
    assert reasoning.text == "let me plan"

    # A subagent invocation surfaces as one `task` call/result pair.
    call = next(e for e in events if isinstance(e, ToolCallStep))
    assert call.tool == "task"
    assert call.call_id == "call_1"
    result = next(e for e in events if isinstance(e, ToolResultStep))
    assert result.tool == "task"
    assert result.call_id == "call_1"
    assert result.is_error is False

    deltas = [e.text for e in events if isinstance(e, MessageDelta)]
    assert deltas == ["Hello ", "world"]
    final = next(e for e in events if isinstance(e, MessageFinal))
    assert final.text == "Hello world"

    usage = next(e for e in events if isinstance(e, RunUsage))
    assert (usage.input_tokens, usage.output_tokens, usage.model) == (10, 5, "scripted")

    structured = next(e for e in events if isinstance(e, StructuredFinal))
    assert structured.data == {"answer": "ok"}

    interrupt = next(e for e in events if isinstance(e, InterruptFinal))
    assert (interrupt.interrupt_id, interrupt.payload) == ("i-1", {"q": "pick"})

    # A fresh turn feeds the built input messages (not a resume Command).
    assert graph.received_input == {"messages": [{"role": "user", "content": "go"}]}


def test_astream_omits_interrupt_read_when_not_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no interrupt_on the paused-state read is skipped, so no InterruptFinal."""
    agent = DeepAgent()
    graph = _FakeCompiledGraph(
        _scripted_chunks(),
        interrupts=[SimpleNamespace(id="i-1", value={"q": "pick"})],
    )
    _install_fake_graph(monkeypatch, agent, graph)

    async def collect() -> list[Any]:
        return [event async for event in agent.astream(user_message=TemplatedText(content="go"), thread_id="t")]

    events = asyncio.run(collect())
    assert not any(isinstance(e, InterruptFinal) for e in events)


def test_astream_resume_feeds_a_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """A resume payload is delivered as a langgraph Command(resume=...)."""
    from langgraph.types import Command

    agent = DeepAgent()
    graph = _FakeCompiledGraph([], interrupts=[])
    _install_fake_graph(monkeypatch, agent, graph)

    async def collect() -> list[Any]:
        return [event async for event in agent.astream(resume={"answer": 1}, thread_id="t")]

    asyncio.run(collect())
    assert isinstance(graph.received_input, Command)
    assert graph.received_input.resume == {"answer": 1}


def test_astream_honors_tool_names(monkeypatch: pytest.MonkeyPatch, app_tools: Any) -> None:
    """astream honors ``tool_names`` (as run does): they resolve to client tools and
    reach the builder combined with live ``tools`` — never silently dropped."""
    app_tools.client_tools["calc"] = _client_tool("calc")
    live = _client_tool("live")

    captured: dict[str, Any] = {}

    async def fake_build_agent(**kwargs: Any) -> tuple[_FakeCompiledGraph, dict[str, Any]]:
        captured.update(kwargs)
        return _FakeCompiledGraph([], interrupts=[]), {"configurable": {"thread_id": "t"}}

    agent: Any = DeepAgent()
    monkeypatch.setattr(agent, "_build_agent", fake_build_agent)

    async def collect() -> list[Any]:
        return [
            event
            async for event in agent.astream(
                user_message=TemplatedText(content="go"), tools=[live], tool_names=["calc"]
            )
        ]

    asyncio.run(collect())
    assert [tool.name for tool in captured["tools"]] == ["live", "calc"]


# ===========================================================================
# Structured-final parity: run and astream agree on the same response_format
# ===========================================================================

_RESPONSE_FORMAT: dict[str, Any] = {
    "title": "N",
    "type": "object",
    "properties": {"n": {"type": "integer"}},
}


def _chunks_without_structured() -> list[tuple[str, Any]]:
    """A scripted run that answers in text but writes no ``structured_response`` —
    so a requested ``response_format`` yields no ``StructuredFinal``."""
    return [("messages", (AIMessageChunk(content="Hello"), {}))]


def test_structured_final_parity_when_structured_is_produced(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the SAME response_format on both faces and a structured result produced:
    ``run`` returns the structured value AND ``astream`` emits one ``StructuredFinal``."""
    run_agent: Any = DeepAgent()
    _install_fake_resolve(monkeypatch, run_agent, _FakeCompiledGraph(_scripted_chunks(), interrupts=[]))
    result = asyncio.run(run_agent.run(user_message=TemplatedText(content="go"), response_format=_RESPONSE_FORMAT))
    assert result == {"answer": "ok"}

    stream_agent = DeepAgent()
    _install_fake_graph(monkeypatch, stream_agent, _FakeCompiledGraph(_scripted_chunks(), interrupts=[]))

    async def collect() -> list[Any]:
        return [
            event
            async for event in stream_agent.astream(
                user_message=TemplatedText(content="go"), response_format=_RESPONSE_FORMAT
            )
        ]

    events = asyncio.run(collect())
    structured = [event for event in events if isinstance(event, StructuredFinal)]
    assert len(structured) == 1
    assert structured[0].data == {"answer": "ok"}


def test_structured_final_parity_when_structured_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the SAME response_format on both faces and NO structured result: ``run``
    raises the missing-structured RuntimeError AND ``astream`` raises the SAME error
    out of the generator (no silent omission of the ``StructuredFinal`` frame)."""
    run_agent: Any = DeepAgent()
    _install_fake_resolve(monkeypatch, run_agent, _FakeCompiledGraph(_chunks_without_structured(), interrupts=[]))
    with pytest.raises(RuntimeError) as run_exc:
        asyncio.run(run_agent.run(user_message=TemplatedText(content="go"), response_format=_RESPONSE_FORMAT))

    stream_agent = DeepAgent()
    _install_fake_graph(monkeypatch, stream_agent, _FakeCompiledGraph(_chunks_without_structured(), interrupts=[]))

    async def drain() -> None:
        async for _ in stream_agent.astream(user_message=TemplatedText(content="go"), response_format=_RESPONSE_FORMAT):
            pass

    with pytest.raises(RuntimeError) as stream_exc:
        asyncio.run(drain())

    # The stream face raises the SAME error the invoke face raises via _drain.
    assert str(stream_exc.value) == str(run_exc.value)
    assert "structured output" in str(stream_exc.value)


def test_astream_nonconforming_structured_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A structured result violating a schema constraint keyword raises loudly from
    the projection's validation step — pinning that ``response_format`` is threaded
    through ``_astream_built`` into the projection."""
    schema = {
        "title": "N",
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 0}},
        "required": ["n"],
    }
    agent = DeepAgent()
    chunks: list[tuple[str, Any]] = [("updates", {"agent": {"structured_response": {"n": -1}}})]
    _install_fake_graph(monkeypatch, agent, _FakeCompiledGraph(chunks, interrupts=[]))
    with pytest.raises(JsonSchemaValidationError):
        _drain_astream(agent.astream(user_message=TemplatedText(content="go"), response_format=schema))


def test_astream_does_not_raise_missing_structured_when_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A paused streaming run surfaces its ``InterruptFinal`` and does NOT raise the
    missing-structured error even with response_format set — the run paused rather
    than finished (mirrors ``_drain``, where an interrupt takes precedence)."""
    agent = DeepAgent()
    graph = _FakeCompiledGraph(
        _chunks_without_structured(), interrupts=[SimpleNamespace(id="i-1", value={"q": "pick"})]
    )
    _install_fake_graph(monkeypatch, agent, graph)

    async def collect() -> list[Any]:
        return [
            event
            async for event in agent.astream(
                user_message=TemplatedText(content="go"), interrupt_on={"task": True}, response_format=_RESPONSE_FORMAT
            )
        ]

    events = asyncio.run(collect())
    assert any(isinstance(event, InterruptFinal) for event in events)
    assert not any(isinstance(event, StructuredFinal) for event in events)


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the agent park index at an in-memory fakeredis so a streamed drive's dead-chain
    tombstone is observable."""
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_park_client() -> AsyncIterator[Any]:
        yield redis

    monkeypatch.setattr(idx, "_park_client", fake_park_client)
    return redis


class _ClaimingGraph:
    """A scripted compiled graph whose FIRST streamed step records a chained-park CLAIM — exactly
    as a nested parking dispatch does, via ``resolve_park_adoption`` under a chained completion —
    then finishes cleanly WITHOUT the agent ever parking on it. The claim is a DEAD CHAIN the
    streaming drive must detach on the way out."""

    def __init__(self, chain_key: str) -> None:
        self.chain_key = chain_key
        self.received_input: Any = None

    async def astream(self, agent_input: Any, config: Any, stream_mode: Any = None) -> AsyncIterator[tuple[str, Any]]:
        self.received_input = agent_input
        # Early step: a dispatched tool drove a nested run that parked, so the caller CHAINS the
        # call — recording the key in whatever claims ledger the streaming face bound for THIS
        # step. Bind agent_resume + the chained completion the way scope_nested_dispatch would.
        completion = set_park_completion(
            CHAINED_PARK_DELIVERY_TOOL_NAME, chained_park_context(self.chain_key, (None, None))
        )
        resume = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
        try:
            key, _owner = resolve_park_adoption(
                "nested_driver_resume", interaction_id="i-nested", tool_name="run_target"
            )
            assert key == self.chain_key
        finally:
            reset_resume_continuation_tool(resume)
            reset_park_completion(completion)
        yield ("updates", {"agent": {"messages": [AIMessage(content="chained the call")]}})
        # Later step: the agent moves on and finishes with a plain answer — it never parks, so the
        # claim is never turned into a park entry and stays a dead chain.
        yield ("messages", (AIMessageChunk(content="done"), {}))

    async def aget_state(self, config: Any, subgraphs: bool = False) -> SimpleNamespace:
        # No pending interrupts: the drive stops without parking, so finalize records nothing.
        task = SimpleNamespace(interrupts=[], state=None)
        return SimpleNamespace(values={}, interrupts=[], tasks=[task])


def test_astream_detaches_a_dead_chain_claimed_mid_stream_without_clobbering_a_live_park(
    monkeypatch: pytest.MonkeyPatch, fake_park_redis: aioredis.FakeRedis
) -> None:
    # The deep-agent STREAMING face owns ONE chained-claims ledger for the whole drive and
    # re-binds it around each step; a claim recorded in an EARLY step must survive to the detach
    # the finally runs when the drive stops without parking. Otherwise the nested run's terminal
    # hunts a park that never exists and is redelivered until the platform's horizon gives up.
    # A live park for another interaction is left untouched — detach writes NX and only over the
    # keys THIS drive claimed. (Reds under a per-step fresh ledger instead of the shared set: the
    # early claim is then discarded at that step's end and the finally detaches nothing.)
    chain_key = new_chained_park_key()
    graph = _ClaimingGraph(chain_key)
    agent: Any = DeepAgent()
    _install_fake_graph(monkeypatch, agent, graph)

    async def go() -> None:
        # A live park entry for a DIFFERENT interaction, pre-written, must be left exactly as is.
        await fake_park_redis.set(idx._park_key("i-live"), '{"thread_id": "t-live"}')
        events = [event async for event in agent.astream(user_message=TemplatedText(content="go"), thread_id="t")]
        assert events
        # The dead chain was tombstoned on the way out — detach fired from the streaming finally.
        entry = await read_park_entry(chain_key)
        assert entry is not None
        assert is_resolved_tombstone(entry)
        # The unrelated live park is untouched.
        assert await read_park_entry("i-live") == {"thread_id": "t-live"}

    asyncio.run(go())
