"""Single-run ``tools_agent`` park/answer: a park on an async ask returns a
suspended receipt, resume drives the parked tool exactly once, and expiry feeds
the expiry marker.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from tai42_contract.interactions import (
    EXPIRY_ANSWER,
    ChainedResume,
    SuspendedInteraction,
    reset_chained_resume,
    reset_park_completion,
    set_chained_resume,
    set_park_completion,
)
from tai42_contract.template import TemplatedText
from tests._tools_agent_park_support import (
    ScriptedChatModel,
    _agent,
    _ask_call,
    _AskStandIn,
    _wire_tools_build,
)
from tests.conftest import bind_park_index

from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import agent_resume
from tai42_agents._internal.park.park_binding import agents_park_index


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
        token = set_park_completion("conversation_deliver", {"thread_id": "bridge:acme:alice"})
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
        result = await agent_resume("i1", EXPIRY_ANSWER)
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
