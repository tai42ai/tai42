"""Single-run ``tools_agent`` park/answer: a park on an async ask returns a
suspended receipt, resume drives the parked tool exactly once, and expiry feeds
the expiry marker.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import orjson
import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    EXPIRY_ANSWER,
    AskAnswer,
    ChainedResume,
    RunTerminalFailed,
    SuspendedInteraction,
    reset_chained_resume,
    reset_park_completion,
    set_chained_resume,
    set_park_completion,
)
from tai42_contract.monitoring import RUN_ATTRIBUTION_TRACE_NAME
from tai42_contract.template import TemplatedText
from tai42_kit.llm.settings import llm_provider_settings
from tai42_kit.monitoring import encode_payload
from tai42_kit.monitoring.testing import span_to_observation

from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import agent_resume
from tai42_agents._internal.park.errors import AgentResumeCheckpointMissingError
from tai42_agents._internal.park.park_binding import agents_park_index

from ._tools_agent_park_support import (
    ScriptedChatModel,
    _agent,
    _ask_call,
    _AskStandIn,
    _wire_tools_build,
)
from .conftest import APP, RecordingMonitoringWriter, bind_park_index


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the park index at a shared in-memory fakeredis and report the park Redis as
    configured (so a run is judged park-capable)."""
    redis = aioredis.FakeRedis(decode_responses=True)
    bind_park_index(monkeypatch, redis)
    return redis


def test_tools_agent_caller_ask_park_carries_both_id_lists(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A caller ask parked inside the agent surfaces on the receipt in BOTH id lists — every parked
    # id, and the caller subset — so the platform's visit partitions caller from user asks. A user
    # ask (the sibling assertion in the test above) carries the caller subset empty.
    saver = InMemorySaver()
    ask = _AskStandIn("i1", caller=True)
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        receipt = await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-caller",
        )
        assert isinstance(receipt, SuspendedInteraction)
        assert receipt.interaction_id == "i1"
        assert receipt.interaction_ids == ["i1"]
        assert receipt.caller_interaction_ids == ["i1"]
        assert receipt.expiry_at == ask._expiry_at

    asyncio.run(go())


def test_tools_agent_park_then_answer_exactly_once(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        receipt = await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-tools",
        )
        assert isinstance(receipt, SuspendedInteraction)
        assert receipt.interaction_id == "i1"
        assert receipt.interaction_ids == ["i1"]
        assert receipt.caller_interaction_ids == []
        assert receipt.expiry_at == ask._expiry_at
        assert ask.calls == 1
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert entry["agent_name"] == "tools_agent"

        # Resume on a fresh runtime: same shared saver stands in for the durable checkpoint.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        # The parked tool ran exactly once — the resume substituted the answer, never re-ran it.
        assert ask.calls == 1
        # A clean drive finalizes the park entry to a resolved tombstone (not an absent key).
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert _is_resolved(entry)

    asyncio.run(go())


def test_tools_agent_resume_over_a_gone_checkpoint_fails_loudly(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The park index still routes the answer, but the thread's checkpoint is gone: the resume ends
    # the run FAILED naming the missing checkpoint, never as a blank clean terminal.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-gone",
        )
        await saver.adelete_thread("t-gone")
        with pytest.raises(RunTerminalFailed) as raised:
            await agent_resume("i1", "the answer")
        assert raised.value.outcome["error_type"] == "AgentResumeCheckpointMissingError"
        assert ask.calls == 1

    asyncio.run(go())


def test_tools_agent_park_bound_keeps_the_redelivery_horizon_inside_the_checkpoint_retention(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The park's bound is measured against the checkpoint the resume reads: an ask whose deadline
    # sits at the bound and whose answer arrives a full redelivery horizon late still lands before
    # that checkpoint's retention runs out.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-bound",
        )
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        checkpoint = await saver.aget_tuple({"configurable": {"thread_id": "t-bound", "checkpoint_ns": ""}})
        assert checkpoint is not None
        written = datetime.fromisoformat(checkpoint.checkpoint["ts"])
        expires = written + timedelta(minutes=llm_provider_settings().checkpoint_retention_waiting_minutes)
        late = datetime.fromisoformat(entry["retention_bound"]) + timedelta(seconds=APP.interactions.redelivery_horizon)
        assert late <= expires

    asyncio.run(go())


def test_a_parked_run_and_its_resume_each_record_their_input_and_output_on_their_run_root(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The parked run's root records the turn and the suspended receipt; the resume opens its own root
    # recording the answers it resumed with and the run's answer.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    writer = cast(RecordingMonitoringWriter, tai42_app.monitoring.active.writer)
    writer.clear()

    agent = _agent()

    async def go() -> tuple[SuspendedInteraction, dict[str, Any]]:
        receipt = await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-roots",
        )
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert await agent_resume("i1", "the answer") == "all done"
        return receipt, {entry["interrupt_id"]: {"i1": "the answer"}}

    receipt, resume_map = asyncio.run(go())

    roots = [span_to_observation(s) for s in writer.spans() if s.parent is None]
    assert [r.name for r in roots] == [RUN_ATTRIBUTION_TRACE_NAME, RUN_ATTRIBUTION_TRACE_NAME]
    parked, resumed = sorted(roots, key=lambda r: r.start or datetime.min.replace(tzinfo=UTC))
    assert parked.input == {"user_message": ["go"]}
    assert parked.output is not None
    assert parked.output["suspended"] == orjson.loads(encode_payload(receipt))
    assert resumed.input == {"resume": resume_map}
    assert resumed.output == "all done"


def test_tools_agent_redrive_after_a_clean_terminal_reproduces_the_answer(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A crash between the terminal drive and the index finalize redelivers the answer onto a thread
    # whose checkpoint holds the finished run and no pending interrupt: the redrive re-produces the
    # same answer from that checkpoint, never re-invoking the graph.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-redrive",
        )
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert await agent_resume("i1", "the answer") == "all done"
        again = await agent.aresume_park(
            rebuild_kwargs=entry["rebuild_kwargs"],
            thread_id="t-redrive",
            resume_map={entry["interrupt_id"]: {"i1": "the answer"}},
        )
        assert again == "all done"
        assert ask.calls == 1

    asyncio.run(go())


def test_tools_agent_run_park_is_outermost_and_captures_no_chain(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # An outermost run-face park (no ancestor driver waiting on it across a chain) captures NO
    # chain routing: its terminal fires nothing, and the platform delivers the run's outcome to
    # the run's own stored address (captured at ask time, not on this driver's park entry). The
    # door's out-of-band address flows DOWN unchanged and is the platform's to fire.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        # The door's out-of-band delivery address is bound and flows down unchanged; the driver
        # does not capture it as the park's chain routing.
        token = set_park_completion("conversation_deliver", thread_id="bridge:acme:alice")
        try:
            receipt = await agent.run(
                tool_names=["ask"],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-run-completion",
            )
        finally:
            reset_park_completion(token)
        assert isinstance(receipt, SuspendedInteraction)

        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert entry["completion_tool"] is None
        assert entry["completion_context"] is None

    asyncio.run(go())


def test_tools_agent_run_park_captures_the_ambient_chain_routing(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A run dispatched as a nested tool of ANOTHER driver's chained call captures that ancestor's
    # chain routing onto its park entry, so its own terminal fires the ancestor's chain-delivery
    # tool and returns the outermost outcome up.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()
    routing = ChainedResume(
        delivery_tool="deliver_chained_park", chain_key="tai42:chained-park:abc", asked_by=("caller",)
    )

    async def go() -> None:
        token = set_chained_resume(routing)
        try:
            receipt = await agent.run(
                tool_names=["ask"],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-run-chained",
            )
        finally:
            reset_chained_resume(token)
        assert isinstance(receipt, SuspendedInteraction)

        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert entry["completion_tool"] == "deliver_chained_park"
        assert entry["completion_context"] == {"chain_key": "tai42:chained-park:abc", "asked_by": ["caller"]}

    asyncio.run(go())


def test_tools_agent_run_park_without_a_completion_binds_none(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Absent an ambient completion the run face still binds the park (the async ask is resumable
    # by agent_resume regardless), with completion_tool=None — byte-identical to the pre-capture
    # behavior. This pins that the capture never fabricates a delivery leg a door did not bind.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        receipt = await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-run-nocompletion",
        )
        assert isinstance(receipt, SuspendedInteraction)
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert entry["completion_tool"] is None
        assert entry["completion_context"] is None

    asyncio.run(go())


def test_a_resume_refuses_a_park_whose_marker_names_no_owner(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # PRODUCTION WIRING (end-to-end): drive the REAL agent_resume over a park whose marker, as the
    # resume's claim check reads it, names no owner. A resume grants no exemption: the ownerless
    # park is refused with a model-visible error, the answer is never substituted.
    from tai42_agents._internal.lc_tools import to_structured_tool
    from tai42_agents._internal.park import middleware as park_mw

    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        receipt = await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-ownerless-e2e",
        )
        assert isinstance(receipt, SuspendedInteraction)

        real_reader = park_mw.read_suspended_interaction_marker

        def ownerless_reader(content: Any) -> Any:
            marker = real_reader(content)
            return None if marker is None else {**marker, "resume_owner": None}

        monkeypatch.setattr(park_mw, "read_suspended_interaction_marker", ownerless_reader)

        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        assert ask.calls == 1

        graph, _ = await base_mod._compile_tools_agent([to_structured_tool(ask.tool())], checkpoint_provider="redis")
        state = await graph.aget_state({"configurable": {"thread_id": "t-ownerless-e2e"}})
        by_call = {m.tool_call_id: m for m in state.values["messages"] if getattr(m, "tool_call_id", None)}
        assert by_call["c1"].status == "error"
        assert "no adoptable owner" in by_call["c1"].content
        assert by_call["c1"].content != "the answer"

    asyncio.run(go())


def test_tools_agent_park_then_expiry_feeds_the_expiry_marker(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    deadline = (datetime.now(UTC) + timedelta(days=2)).replace(microsecond=0)
    ask = _AskStandIn("i1", expiry_at=deadline)
    model = ScriptedChatModel([_ask_call(), AIMessage(content="expired path")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        receipt = await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-expiry",
        )
        assert receipt.expiry_at == deadline
        expired = AskAnswer(answer=EXPIRY_ANSWER, reply_to=None, params=None).model_dump(mode="json")
        result = await agent_resume("i1", expired)
        assert result == "expired path"
        # A clean drive finalizes the park entry to a resolved tombstone (not an absent key).
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert _is_resolved(entry)

    asyncio.run(go())


def test_tools_agent_park_has_no_interrupt_on_collision(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # tools_agent wires no HITL interrupt_on, so the ONLY pending interrupt a super-step can
    # raise is the async-ask park — there is nothing for it to collide with. The park resumes
    # cleanly by its own id.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-hitl",
        )
        entry = await agents_park_index().read_entry("i1")
        # Exactly one park interrupt id was stored — no second (HITL) interrupt exists.
        assert entry is not None
        assert isinstance(entry["interrupt_id"], str)
        result = await agent_resume("i1", "answer")
        assert result == "done"

    asyncio.run(go())


def _is_resolved(entry: Any) -> bool:
    return agents_park_index().tombstone_kind(entry) == "resolved"


async def _latest_checkpoint_id(saver: InMemorySaver, thread_id: str) -> str:
    latest = await saver.aget_tuple({"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}})
    assert latest is not None
    return latest.checkpoint["id"]


def _pin_kwargs(pin_by: str, checkpoint_id: str) -> dict[str, Any]:
    if pin_by == "resume_checkpoint_id":
        return {"resume_checkpoint_id": checkpoint_id}
    return {"langgraph_config": {"configurable": {"checkpoint_id": checkpoint_id}}}


@pytest.mark.parametrize("pin_by", ["resume_checkpoint_id", "langgraph_config"])
def test_tools_agent_run_forked_from_a_pinned_checkpoint_parks_and_resumes(
    pin_by: str, fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A turn forked from an earlier checkpoint that async-asks parks at the checkpoint the drive
    # ended at: the receipt is returned, the park entry is persisted, and the answer resumes it.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([AIMessage(content="turn one"), _ask_call(), AIMessage(content="park answered")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        await agent.run(
            tool_names=["ask"], checkpoint_provider="redis", user_message=TemplatedText(content="hi"), thread_id="t-pin"
        )
        pin = await _latest_checkpoint_id(saver, "t-pin")
        parked = await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-pin",
            **_pin_kwargs(pin_by, pin),
        )
        assert isinstance(parked, SuspendedInteraction)
        assert await agents_park_index().read_entry("i1") is not None
        assert await agent_resume("i1", "the answer") == "park answered"
        assert ask.calls == 1

    asyncio.run(go())


def test_tools_agent_resume_of_a_pinned_park_over_a_gone_checkpoint_fails_loudly(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The pinned checkpoint id the park was started from names nothing once the thread is gone:
    # the resume still ends FAILED naming the missing checkpoint.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([AIMessage(content="turn one"), _ask_call(), AIMessage(content="park answered")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        await agent.run(
            tool_names=["ask"], checkpoint_provider="redis", user_message=TemplatedText(content="hi"), thread_id="t-pg"
        )
        pin = await _latest_checkpoint_id(saver, "t-pg")
        await agent.run(
            tool_names=["ask"],
            checkpoint_provider="redis",
            user_message=TemplatedText(content="go"),
            thread_id="t-pg",
            langgraph_config={"configurable": {"checkpoint_id": pin}},
        )
        await saver.adelete_thread("t-pg")
        with pytest.raises(RunTerminalFailed) as raised:
            await agent_resume("i1", "the answer")
        assert raised.value.outcome["error_type"] == "AgentResumeCheckpointMissingError"

    asyncio.run(go())


@pytest.mark.parametrize("face", ["run", "astream"])
def test_tools_agent_resume_input_over_a_thread_with_no_checkpoint_raises(
    face: str, fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A ``resume`` answer aimed at a thread that holds no checkpoint is refused before the graph
    # runs, never driven as a fresh run over empty state.
    saver = InMemorySaver()
    model = ScriptedChatModel([AIMessage(content="fresh run answer")])
    _wire_tools_build(monkeypatch, model, saver)

    agent = _agent()

    async def go() -> None:
        async def drive() -> Any:
            if face == "run":
                return await agent.run(checkpoint_provider="redis", resume={"answer": 1}, thread_id="t-none")
            return [
                event
                async for event in agent.astream(checkpoint_provider="redis", resume={"answer": 1}, thread_id="t-none")
            ]

        with pytest.raises(AgentResumeCheckpointMissingError) as raised:
            await drive()
        assert raised.value.thread_id == "t-none"
        assert await saver.aget_tuple({"configurable": {"thread_id": "t-none", "checkpoint_ns": ""}}) is None

    asyncio.run(go())
