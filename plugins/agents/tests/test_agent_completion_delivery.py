"""Completion delivery firing on a clean terminal drive and on permanent
abandonment, and the no-override / silent-noop / chained-failed cases.
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
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
)
from tests._agent_completion_support import (
    _COMPLETION_TOOL,
    ScriptedChatModel,
    _agent,
    _ask_call,
    _completion_context,
    _expected_delivery,
    _expected_failed_delivery,
    _park_via_astream,
    _SequentialAsk,
    _wire,
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


def test_completion_fires_on_terminal_drive(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["completion_tool"] == _COMPLETION_TOOL
        # The binder's OPAQUE context rides the durable entry beside the tool name, so the fire
        # can address the delivery tool by ITS own routing parameter.
        assert entry["completion_context"] == _completion_context("bridge:acme:alice")

        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        # The completion tool fired ONCE, awaited, with the generic contract payload: the bound
        # context, the final answer, the stable completion id, and the succeeded status.
        assert delivered == [_expected_delivery("bridge:acme:alice", ["i1"], "all done")]
        # A clean drive finalizes the entry to a resolved tombstone AFTER the completion handoff.
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_failed_completion_fires_once_on_permanent_abandonment(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A park whose resume the platform PERMANENTLY gave up on (never drove cleanly) is closed by
    # the abandonment fire: the bound completion tool fires once with the FAILED terminal, under
    # the same stable completion id a success would have used, so the bound caller is told the
    # answer is never coming instead of waiting to its own deadline.
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")
        # The park exists and was never driven — no completion has fired yet.
        assert delivered == []

        await park_resume.fire_park_failed_completion("i1")
        assert delivered == [_expected_failed_delivery("bridge:acme:alice", ["i1"])]

        # A second abandonment fire (e.g. a sibling's redelivery, or a re-run reaper pass) re-fires
        # under the SAME completion id, so a delivery ledger keyed on that id collapses it to one
        # record — the driver does not tombstone; the id is the exactly-once authority.
        await park_resume.fire_park_failed_completion("i1")
        assert [d["completion_id"] for d in delivered] == [
            park_resume._completion_id("bridge:acme:alice", idx.compute_superstep_id(["i1"])),
            park_resume._completion_id("bridge:acme:alice", idx.compute_superstep_id(["i1"])),
        ]
        assert all(d["status"] == PARK_COMPLETION_FAILED for d in delivered)

    asyncio.run(go())


def test_abandonment_after_a_clean_success_never_overrides_it(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The critical safety pin: a super-step that drove cleanly (a delivered SUCCESS, resolved
    # tombstone) must NEVER have a FAILED terminal fired over it by a late abandonment. The
    # tombstone guard makes the abandonment fire a no-op.
    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")
        assert await agent_resume("i1", "the answer") == "all done"
        assert delivered == [_expected_delivery("bridge:acme:alice", ["i1"], "all done")]
        assert idx.is_resolved_tombstone(await idx.read_park_entry("i1") or {})

        # The resume already delivered SUCCESS; a permanent-give-up notice arriving afterward is a
        # no-op — the FAILED terminal is never delivered over the success.
        await park_resume.fire_park_failed_completion("i1")
        assert delivered == [_expected_delivery("bridge:acme:alice", ["i1"], "all done")]

    asyncio.run(go())


def test_abandonment_of_an_absent_park_is_a_silent_noop(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A park whose own entry TTL has lapsed (nothing left to deliver against) yields a clean
    # no-op, never a raise — the abandonment fire is best-effort at the terminus.
    saver = InMemorySaver()
    _wire(monkeypatch, ScriptedChatModel([]), saver)
    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    async def go() -> None:
        await park_resume.fire_park_failed_completion("nonexistent")
        assert delivered == []

    asyncio.run(go())


def test_abandonment_of_a_chained_park_delivers_the_failed_terminal_to_the_chain(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The chained-park tail-drop closes at the SAME exhaustion point: a NESTED run parks with its
    # completion tool bound to ``deliver_chained_park`` (nested_dispatch binds it), so when the
    # nested run's resume is permanently abandoned, the abandonment fire delivers the FAILED
    # terminal into the chained delivery tool — the outer run waiting on the CALL learns the call
    # came back empty instead of waiting to its own deadline.
    from tai42_agents._internal.park.chain import CHAINED_PARK_DELIVERY_TOOL_NAME

    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[CHAINED_PARK_DELIVERY_TOOL_NAME] = lambda **kwargs: delivered.append(kwargs)

    thread_id = "bridge:acme:alice"
    chain_token = "tai42:chained-park:abc"
    # A live nested park entry (never driven), shaped as persist_park writes it, whose completion
    # tool is the chained delivery tool and whose context carries the chain token the outer run
    # parked on.
    entry = {
        "agent_name": "tools_agent",
        "thread_id": thread_id,
        "superstep_id": idx.compute_superstep_id(["nested-i1"]),
        "interrupt_id": "irq-1",
        "rebuild_kwargs": {},
        "completion_tool": CHAINED_PARK_DELIVERY_TOOL_NAME,
        "completion_context": {"chain_token": chain_token},
        "retention_bound": None,
        "execution_identity": None,
        "execution_fingerprint": "",
    }

    async def go() -> None:
        await fake_park_redis.set(idx._park_key("nested-i1"), json.dumps(entry))
        await park_resume.fire_park_failed_completion("nested-i1")
        assert delivered == [
            {
                "chain_token": chain_token,
                "result": None,
                "completion_id": park_resume._completion_id(thread_id, idx.compute_superstep_id(["nested-i1"])),
                "status": PARK_COMPLETION_FAILED,
            }
        ]

    asyncio.run(go())
