"""Completion binding carried forward across a re-park and the deep-agent handoff:
context fallback, pre-upgrade addresses, and crash-then-redelivery once.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    PARK_COMPLETION_SUCCEEDED,
    reset_park_completion,
    set_park_completion,
)
from tai42_contract.template import TemplatedText
from tests._agent_completion_support import (
    _COMPLETION_TOOL,
    _WITHIN_HORIZON,
    ScriptedChatModel,
    _agent,
    _ask_call,
    _completion_context,
    _expected_delivery,
    _park_via_astream,
    _park_via_astream_deep,
    _SequentialAsk,
    _wire,
    _wire_deep,
)

from tai42_agents._internal.park import agent_resume
from tai42_agents._internal.park import capability as park_capability
from tai42_agents._internal.park import index as idx
from tai42_agents._internal.park import resume as park_resume


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_park_client() -> AsyncIterator[Any]:
        yield redis

    settings = SimpleNamespace(redis_url="redis://fake")
    monkeypatch.setattr(idx, "_park_client", fake_park_client)
    monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: settings)
    monkeypatch.setattr(park_capability, "agents_park_redis_settings", lambda: settings)
    return redis


def test_completion_carried_forward_on_repark(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1", "i2"])
    model = ScriptedChatModel([_ask_call("c1"), _ask_call("c2"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")

        # First resume RE-PARKS on the second ask; the completion binding (tool AND context) is
        # carried forward to the new entry, and is NOT fired on a re-park.
        receipt = await agent_resume("i1", "a1")
        assert receipt["status"] == "suspended"
        assert receipt["interaction_ids"] == ["i2"]
        assert delivered == []
        new_entry = await idx.read_park_entry("i2")
        assert new_entry is not None
        assert new_entry["completion_tool"] == _COMPLETION_TOOL
        assert new_entry["completion_context"] == _completion_context("bridge:acme:alice")

        # Second resume terminates; the completion fires once with the final answer, keyed by the
        # SECOND super-step (the one that resolved).
        result = await agent_resume("i2", "a2")
        assert result == "all done"
        assert delivered == [_expected_delivery("bridge:acme:alice", ["i2"], "all done")]

    asyncio.run(go())


def test_completion_fire_carries_only_the_bound_context(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # GENERIC: the driver never fabricates a routing argument. A binder that wires a completion
    # tool with NO context gets a fire carrying the terminal outcome alone — the delivery tool's
    # parameters are the BINDER's business, never this driver's.
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = _agent()

    async def go() -> None:
        token = set_park_completion(_COMPLETION_TOOL)
        try:
            async for _event in agent.astream(
                tool_names=["ask"],
                checkpoint_provider="redis",
                user_message=TemplatedText(content="go"),
                thread_id="bridge:acme:alice",
            ):
                pass
        finally:
            reset_park_completion(token)
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["completion_context"] is None

        assert await agent_resume("i1", "the answer") == "all done"
        completion_id = park_resume._completion_id("bridge:acme:alice", idx.compute_superstep_id(["i1"]))
        assert delivered == [
            {"result": "all done", "completion_id": completion_id, "status": PARK_COMPLETION_SUCCEEDED}
        ]

    asyncio.run(go())


def test_completion_context_falls_back_to_the_parked_thread_on_a_fieldless_entry() -> None:
    # A stored entry predating the field carries no ``completion_context`` at all: the fallback
    # reproduces exactly what the old driver injected, so an in-flight AGENT-route park keeps its
    # address instead of dropping its answer.
    assert park_resume._completion_context({"thread_id": "bridge:acme:alice"}, "bridge:acme:alice") == {
        "thread_id": "bridge:acme:alice"
    }
    # PRESENCE is what is tested, so a field that IS present is authoritative — including an
    # explicit ``None`` (the binder wired no routing context), which never resurrects the
    # thread fallback.
    assert park_resume._completion_context({"completion_context": {"delivery_thread_id": "t"}}, "th") == {
        "delivery_thread_id": "t"
    }
    assert park_resume._completion_context({"completion_context": None}, "th") is None


def test_pre_upgrade_entry_keeps_its_address_across_a_repark(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # THROUGH the re-park path: an in-flight park persisted BEFORE the context field existed is
    # answered, re-parks on a second ask, and only then terminates. The rebind around the resume
    # drive must read the SAME pre-upgrade fallback the fire does — a rebind that read the absent
    # field as "no context" would re-persist an address-less entry and the terminal fire, one
    # re-park later, would carry no thread_id at all and the answer would be dropped.
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1", "i2"])
    model = ScriptedChatModel([_ask_call("c1"), _ask_call("c2"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")

        # Age the stored entry back to its pre-upgrade shape: the field is not None, it is ABSENT.
        key = idx._park_key("i1")
        entry = json.loads(await fake_park_redis.get(key))
        entry.pop("completion_context")
        await fake_park_redis.set(key, json.dumps(entry))

        receipt = await agent_resume("i1", "a1")
        assert receipt["status"] == "suspended"
        assert delivered == []

        result = await agent_resume("i2", "a2")
        assert result == "all done"
        # The fire still carries the parked thread as its delivery address.
        assert delivered == [_expected_delivery("bridge:acme:alice", ["i2"], "all done")]

    asyncio.run(go())


def test_langchain_deep_agent_completion_carried_forward_on_repark(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    store = InMemoryStore()
    ask = _SequentialAsk(["i1", "i2"], expiry_at=_WITHIN_HORIZON)
    model = ScriptedChatModel([_ask_call("c1"), _ask_call("c2"), AIMessage(content="all done")])
    _wire_deep(monkeypatch, model, saver, store)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> None:
        await _park_via_astream_deep(agent, "bridge:acme:alice")

        # First resume RE-PARKS on the second ask; the completion tool must be carried forward
        # to the new entry (the langchain_deep_agent resume face rebinds it), and is NOT fired on a re-park.
        receipt = await agent_resume("i1", "a1")
        assert receipt["status"] == "suspended"
        assert receipt["interaction_ids"] == ["i2"]
        assert delivered == []
        new_entry = await idx.read_park_entry("i2")
        assert new_entry is not None
        assert new_entry["completion_tool"] == _COMPLETION_TOOL

        # Second resume terminates; the completion fires once with the final answer, keyed by the
        # SECOND super-step (the one that resolved).
        result = await agent_resume("i2", "a2")
        assert result == "all done"
        assert delivered == [_expected_delivery("bridge:acme:alice", ["i2"], "all done")]

    asyncio.run(go())


def test_langchain_deep_agent_completion_handoff_crash_then_redelivery_delivers_once(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    store = InMemoryStore()
    ask = _SequentialAsk(["i1"], expiry_at=_WITHIN_HORIZON)
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire_deep(monkeypatch, model, saver, store)
    app_tools.client_tools["ask"] = ask.tool()

    calls: list[dict[str, Any]] = []

    def _flaky(**kwargs: Any) -> None:
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("delivery backend down")

    app_tools.tool_runners[_COMPLETION_TOOL] = _flaky

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> None:
        await _park_via_astream_deep(agent, "bridge:acme:alice")
        superstep_id = idx.compute_superstep_id(["i1"])

        with pytest.raises(RuntimeError, match="delivery backend down"):
            await agent_resume("i1", "the answer")
        assert not idx.is_resolved_tombstone(await idx.read_park_entry("i1") or {})
        await fake_park_redis.delete(idx._claim_key("bridge:acme:alice", superstep_id))

        # Redelivery re-drives the already-terminal graph idempotently (re-produced from the
        # persisted state, never re-invoked) and re-fires the handoff under the SAME stable id.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        assert [c["completion_id"] for c in calls] == [
            park_resume._completion_id("bridge:acme:alice", superstep_id),
            park_resume._completion_id("bridge:acme:alice", superstep_id),
        ]
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_completion_handoff_failure_raises_and_leaves_index_live(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    def _boom(**kwargs: Any) -> None:
        raise RuntimeError("delivery backend down")

    app_tools.tool_runners[_COMPLETION_TOOL] = _boom

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")
        # The drive succeeds, but the completion handoff is AWAITED before finalize: its failure
        # raises out of the resume and the index is NOT finalized, so the platform keeps the
        # due-record and a redelivery re-drives and re-reaches the handoff.
        with pytest.raises(RuntimeError, match="delivery backend down"):
            await agent_resume("i1", "the answer")
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert not idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_completion_handoff_crash_then_redelivery_delivers_once(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    calls: list[dict[str, Any]] = []

    def _flaky(**kwargs: Any) -> None:
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("delivery backend down")

    app_tools.tool_runners[_COMPLETION_TOOL] = _flaky

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")
        superstep_id = idx.compute_superstep_id(["i1"])

        # First drive succeeds, handoff crashes → resume raises, index left LIVE, no finalize.
        with pytest.raises(RuntimeError, match="delivery backend down"):
            await agent_resume("i1", "the answer")
        assert not idx.is_resolved_tombstone(await idx.read_park_entry("i1") or {})
        # The crashed winner's lease lapses (simulated), so a redelivery reclaims and re-drives.
        await fake_park_redis.delete(idx._claim_key("bridge:acme:alice", superstep_id))

        # Redelivery re-drives idempotently and re-reaches the handoff, which now succeeds.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        # Both drives fired the handoff under the SAME stable completion id, so a delivery
        # ledger keyed on that id collapses the redelivery to a single record.
        assert [c["completion_id"] for c in calls] == [
            park_resume._completion_id("bridge:acme:alice", superstep_id),
            park_resume._completion_id("bridge:acme:alice", superstep_id),
        ]
        # The successful drive finalized the entry to a resolved tombstone.
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())
