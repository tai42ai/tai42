"""Abandonment binds the park's recorded execution identity, skips while a live
drive holds the lease, and registers its handler with the resume tool.
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
from tests._agent_completion_support import (
    _COMPLETION_TOOL,
    ScriptedChatModel,
    _agent,
    _ask_call,
    _completion_context,
    _expected_failed_delivery,
    _park_via_astream,
    _SequentialAsk,
    _wire,
)

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


def test_abandonment_binds_the_parks_recorded_execution_identity(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # PROPER-AUTHZ: the park records the execution identity its run is authorized as, and the
    # abandonment fire binds it (via the contract's host-registered binder) so the completion
    # dispatches UNDER that identity rather than fail-open. Proven end to end: a fake accessor is
    # what build_park_identity captures at park time, and a fake binder records the bind + whether
    # the fire ran inside it.
    from tai42_contract.interactions import continuation as cont

    monkeypatch.setattr(cont, "_execution_identity_accessor", lambda: ("user-x", "fp-x"))
    bound: list[tuple[str | None, str]] = []
    active = {"in": False}

    @contextlib.asynccontextmanager
    async def _fake_binder(key: str, fingerprint: str) -> AsyncIterator[None]:
        bound.append((key, fingerprint))
        active["in"] = True
        try:
            yield
        finally:
            active["in"] = False

    monkeypatch.setattr(cont, "_execution_identity_binder", _fake_binder)

    saver = InMemorySaver()
    ask = _SequentialAsk(["i1"])
    model = ScriptedChatModel([_ask_call("c1"), AIMessage(content="all done")])
    _wire(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()
    fired_bound: list[bool] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: fired_bound.append(active["in"])

    agent = _agent()

    async def go() -> None:
        await _park_via_astream(agent, "bridge:acme:alice")
        # The identity was captured onto the durable entry at park time.
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["execution_identity"] == "user-x"
        assert entry["execution_fingerprint"] == "fp-x"

        await park_resume.fire_park_failed_completion("i1")
        # The fire bound the recorded identity, and the completion ran WHILE it was bound.
        assert bound == [("user-x", "fp-x")]
        assert fired_bound == [True]

    asyncio.run(go())


def test_abandonment_of_a_chained_park_binds_identity_around_the_chain_drive(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The chained path binds identity too: a nested run's abandonment fires deliver_chained_park
    # (which drives the outer agent_resume) UNDER the recorded identity, never fail-open.
    from tai42_contract.interactions import continuation as cont

    from tai42_agents._internal.park.chain import CHAINED_PARK_DELIVERY_TOOL_NAME

    bound: list[tuple[str | None, str]] = []
    active = {"in": False}

    @contextlib.asynccontextmanager
    async def _fake_binder(key: str, fingerprint: str) -> AsyncIterator[None]:
        bound.append((key, fingerprint))
        active["in"] = True
        try:
            yield
        finally:
            active["in"] = False

    monkeypatch.setattr(cont, "_execution_identity_binder", _fake_binder)

    fired_bound: list[bool] = []
    app_tools.tool_runners[CHAINED_PARK_DELIVERY_TOOL_NAME] = lambda **kwargs: fired_bound.append(active["in"])

    entry = {
        "agent_name": "tools_agent",
        "thread_id": "bridge:acme:alice",
        "superstep_id": idx.compute_superstep_id(["nested-i1"]),
        "interrupt_id": "irq-1",
        "rebuild_kwargs": {},
        "completion_tool": CHAINED_PARK_DELIVERY_TOOL_NAME,
        "completion_context": {"chain_token": "tai42:chained-park:abc"},
        "retention_bound": None,
        "execution_identity": "user-outer",
        "execution_fingerprint": "fp-outer",
    }

    async def go() -> None:
        await fake_park_redis.set(idx._park_key("nested-i1"), json.dumps(entry))
        await park_resume.fire_park_failed_completion("nested-i1")
        assert bound == [("user-outer", "fp-outer")]
        assert fired_bound == [True]

    asyncio.run(go())


def test_entry_without_recorded_identity_fires_unbound(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # An entry without a recorded execution identity fires its completion UNBOUND (the binder is
    # never entered) — never a crash, and never a silent drop of the answer.
    from tai42_contract.interactions import continuation as cont

    bound: list[tuple[str | None, str]] = []

    @contextlib.asynccontextmanager
    async def _fake_binder(key: str, fingerprint: str) -> AsyncIterator[None]:
        bound.append((key, fingerprint))
        yield

    monkeypatch.setattr(cont, "_execution_identity_binder", _fake_binder)

    delivered: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: delivered.append(kwargs)

    # An entry carrying every current field EXCEPT the identity pair.
    entry = {
        "agent_name": "tools_agent",
        "thread_id": "bridge:acme:alice",
        "superstep_id": idx.compute_superstep_id(["i1"]),
        "interrupt_id": "irq-1",
        "rebuild_kwargs": {},
        "completion_tool": _COMPLETION_TOOL,
        "completion_context": _completion_context("bridge:acme:alice"),
        "retention_bound": None,
    }

    async def go() -> None:
        await fake_park_redis.set(idx._park_key("i1"), json.dumps(entry))
        await park_resume.fire_park_failed_completion("i1")
        # Unbound: the binder was never entered, yet the completion still fired.
        assert bound == []
        assert delivered == [_expected_failed_delivery("bridge:acme:alice", ["i1"])]

    asyncio.run(go())


def test_abandonment_skipped_while_a_live_drive_holds_the_lease(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A live drive holding the super-step lease may still deliver SUCCESS, so the abandonment fire
    # must NOT race it: with the claim held the FAILED fire is skipped; once released (and the park
    # still unresolved) it fires.
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
        superstep_id = idx.compute_superstep_id(["i1"])

        # A concurrent drive holds the lease: abandonment skips the FAILED fire.
        assert await idx.try_claim_drive("bridge:acme:alice", superstep_id, "other-drive")
        await park_resume.fire_park_failed_completion("i1")
        assert delivered == []

        # The drive released without resolving (its own error path): abandonment now fires.
        await idx.release_claim("bridge:acme:alice", superstep_id, "other-drive")
        await park_resume.fire_park_failed_completion("i1")
        assert delivered == [_expected_failed_delivery("bridge:acme:alice", ["i1"])]

    asyncio.run(go())


def test_abandonment_of_a_run_face_park_with_no_completion_tool_is_a_noop(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A run-face park records no completion tool (its caller receives the resumed result directly),
    # so there is no out-of-band delivery path — the abandonment fire is a clean no-op.
    fired: list[dict[str, Any]] = []
    app_tools.tool_runners[_COMPLETION_TOOL] = lambda **kwargs: fired.append(kwargs)

    entry = {
        "agent_name": "tools_agent",
        "thread_id": "bridge:acme:alice",
        "superstep_id": idx.compute_superstep_id(["i1"]),
        "interrupt_id": "irq-1",
        "rebuild_kwargs": {},
        "completion_tool": None,
        "completion_context": None,
        "retention_bound": None,
        "execution_identity": None,
        "execution_fingerprint": "",
    }

    async def go() -> None:
        await fake_park_redis.set(idx._park_key("i1"), json.dumps(entry))
        await park_resume.fire_park_failed_completion("i1")
        assert fired == []
        # The claim guard must not have leaked a lease on the no-op path.
        assert not await fake_park_redis.exists(idx._claim_key("bridge:acme:alice", idx.compute_superstep_id(["i1"])))

    asyncio.run(go())


def test_abandonment_handler_is_registered_with_the_resume_tool() -> None:
    # The abandonment handler is registered alongside the resume continuation, so the platform's
    # give-up notice reaches the driver. Idempotent by identity: re-registering is a no-op.
    from tai42_contract.interactions import continuation as cont

    from tai42_agents._internal.park.resume_tool import register_agent_resume_tool

    register_agent_resume_tool()
    before = list(cont._continuation_abandonment_handlers)
    assert park_resume.fire_park_failed_completion in before
    register_agent_resume_tool()
    assert list(cont._continuation_abandonment_handlers) == before
