"""Single-run ``tools_agent`` park/answer: a park on an async ask returns a
suspended receipt, resume drives the parked tool exactly once, and expiry feeds
the expiry marker.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from tai42_contract.interactions import (
    EXPIRY_ANSWER,
    reset_park_completion,
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

from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import agent_resume
from tai42_agents._internal.park import capability as park_capability
from tai42_agents._internal.park import index as idx


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the park index at a shared in-memory fakeredis and report the park Redis as
    configured (so a run is judged park-capable)."""
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_park_client() -> AsyncIterator[Any]:
        yield redis

    settings = SimpleNamespace(redis_url="redis://fake")
    monkeypatch.setattr(idx, "_park_client", fake_park_client)
    monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: settings)
    monkeypatch.setattr(park_capability, "agents_park_redis_settings", lambda: settings)
    return redis


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
        assert receipt == {
            "status": "suspended",
            "interaction_ids": ["i1"],
            "thread_id": "t-tools",
            "expiry_at": None,
        }
        assert ask.calls == 1
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["agent_name"] == "tools_agent"

        # Resume on a fresh runtime: same shared saver stands in for the durable checkpoint.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        # The parked tool ran exactly once — the resume substituted the answer, never re-ran it.
        assert ask.calls == 1
        # A clean drive finalizes the park entry to a resolved tombstone (not an absent key).
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_tools_agent_run_park_captures_the_ambient_completion(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The run face is a JSON tool-face a flow node / conversation agent turn dispatches. When
    # that door bound a park completion around the dispatch (the deferred-answer DELIVERY leg),
    # a park raised inside the run must CAPTURE it — in parity with the astream face — so the
    # resumed run's final answer has a path back to the door. On origin/main the run face binds
    # the park with completion_tool=None (no delivery leg), so agent_resume drives the answer to
    # NOWHERE and the participant is orphaned; this pins the capture that makes the park deliverable.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()
    completion_tool = "conversation_deliver"
    completion_context = {"thread_id": "bridge:acme:alice"}

    async def go() -> None:
        # Stand in for the door that owns delivery binding its completion around the dispatch.
        token = set_park_completion(completion_tool, completion_context)
        try:
            receipt = await agent.run(
                tool_names=["ask"],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="t-run-completion",
            )
        finally:
            reset_park_completion(token)
        assert receipt["status"] == "suspended"

        # The park entry carries the door's delivery leg verbatim, so a resumed clean terminal
        # fires the completion back to the address the door bound (never delivered nowhere).
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["completion_tool"] == completion_tool
        assert entry["completion_context"] == completion_context

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
        assert receipt["status"] == "suspended"
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["completion_tool"] is None
        assert entry["completion_context"] is None

    asyncio.run(go())


def test_tools_agent_resume_delivers_a_legacy_ownerless_park_answer_end_to_end(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # PRODUCTION WIRING (end-to-end): a park written by a release predecessor carries no
    # resume_owner on its persisted wire marker. Drive the REAL agent_resume over such a park and
    # prove the operator's answer is SUBSTITUTED into the parked ToolMessage, not refused/dropped.
    # This pins the driver's ``resuming_park_interaction_ids(frozenset(expected))`` wrap: removing
    # it leaves the ownerless marker refused and the answer replaced by a tool error (red).
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
            thread_id="t-legacy-e2e",
        )
        assert receipt["status"] == "suspended"

        # Downgrade every marker the resume middleware reads to the legacy TWO-KEY wire form (no
        # resume_owner key), standing in for a park persisted by a released predecessor.
        real_reader = park_mw.read_suspended_interaction_marker

        def legacy_reader(content: Any) -> Any:
            marker = real_reader(content)
            return None if marker is None else {k: v for k, v in marker.items() if k != "resume_owner"}

        monkeypatch.setattr(park_mw, "read_suspended_interaction_marker", legacy_reader)

        # The real driver rebuilds the graph and resumes it, naming i1 as the interaction it is
        # delivering an answer for — so the ownerless in-flight park is claimed, not refused.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        assert ask.calls == 1

        # The parked ToolMessage carries the operator's ANSWER, read back off the shared checkpoint
        # — never a refusal error (which is what a dropped answer would leave).
        graph = await base_mod._compile_tools_agent([ask.tool()], checkpoint_provider="redis")
        state = await graph.aget_state({"configurable": {"thread_id": "t-legacy-e2e"}})
        by_call = {m.tool_call_id: m for m in state.values["messages"] if getattr(m, "tool_call_id", None)}
        assert by_call["c1"].content == "the answer"
        assert getattr(by_call["c1"], "status", None) != "error"

    asyncio.run(go())


def test_tools_agent_park_then_expiry_feeds_the_expiry_marker(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    deadline = datetime(2030, 1, 1, tzinfo=UTC)
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
        assert receipt["expiry_at"] == deadline.isoformat()
        result = await agent_resume("i1", EXPIRY_ANSWER)
        assert result == "expired path"
        # A clean drive finalizes the park entry to a resolved tombstone (not an absent key).
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

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
        entry = await idx.read_park_entry("i1")
        # Exactly one park interrupt id was stored — no second (HITL) interrupt exists.
        assert entry is not None
        assert isinstance(entry["interrupt_id"], str)
        result = await agent_resume("i1", "answer")
        assert result == "done"

    asyncio.run(go())
