"""The completion-continuation: a resumed run's FINAL answer delivered out of band.

When a completion tool and its opaque routing context are bound around a park-capable
``astream`` run, the park entry stores both and a CLEAN TERMINAL drive AWAITS
``run_tool(completion_tool, {**context, result, completion_id, status})`` — the generic
completion-fire payload the contract pins — with the final answer BEFORE finalizing the index,
so the durable delivery commit precedes the tombstone. A re-park carries the completion binding
forward to the new entry (fired only when the run finally terminates). A handoff failure raises
loudly and leaves the index LIVE, so the platform redelivers and re-drives — the completion is
never dropped.

Driven over a real ``tools_agent`` / ``langchain_deep_agent`` graph with a scripted model + shared
in-memory checkpointer and the fakeredis-backed park index (same wiring as the park tests).

The fired payload's agreement with the skeleton delivery tool that receives it is a CROSS-package
contract neither side's suite can see on its own; it is pinned end to end by the e2e bridge spec
``test_agent_target_park_deliver`` (this package may not import tai42_skeleton outside the one
exempted boot test, so the pair can only meet in a spec that runs both).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    get_resume_continuation_tool,
    reset_park_completion,
    set_park_completion,
    suspended_interaction_marker,
)

from tai42_agents import tools_agent as tools_mod
from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import agent_resume
from tai42_agents._internal.park import driver as drv
from tai42_agents._internal.park import index as idx
from tai42_agents.langchain_deep_agent import agent as agent_mod

_COMPLETION_TOOL = "conversation_deliver"


def _completion_context(thread_id: str) -> dict[str, Any]:
    """The opaque routing context a completion binder pairs with the tool — the delivery address
    the tool reads, keyed by ITS own parameter. Mirrors what the conversation door binds around
    an agent turn; the driver carries it verbatim and never interprets it."""
    return {"thread_id": thread_id}


class ScriptedChatModel(BaseChatModel):
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


# Within the durable-workspace retention horizon (session_ttl, 24h default) — the deep agent's
# parks must carry a deadline inside it.
_WITHIN_HORIZON = datetime.now(UTC) + timedelta(hours=1)


class _SequentialAsk:
    """A tool that parks on a fresh interaction id each call (so a run can park, resume, then
    re-park on a second ask). Refuses loudly if no resume continuation is bound."""

    def __init__(self, ids: list[str], expiry_at: datetime | None = None) -> None:
        self.calls = 0
        self._ids = ids
        # The durable ``langchain_deep_agent`` acquires a persistent workspace whose TTL bounds
        # the park retention to min(checkpoint, workspace) (§B3.1), so its parks must carry an ask
        # deadline within that horizon; the non-durable ``tools_agent`` keeps a keep-forever
        # (None) retention and its parks pass with no deadline.
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            if get_resume_continuation_tool() is None:
                raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
            interaction_id = self._ids[self.calls]
            self.calls += 1
            return suspended_interaction_marker(interaction_id, self._expiry_at, get_resume_continuation_tool())

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _ask_call(call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "ask", "args": {}}])


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_park_client() -> AsyncIterator[Any]:
        yield redis

    settings = SimpleNamespace(redis_url="redis://fake")
    monkeypatch.setattr(idx, "_park_client", fake_park_client)
    monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: settings)
    monkeypatch.setattr(drv, "agents_park_redis_settings", lambda: settings)
    return redis


class _Registry:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def get_checkpointer(self, **kwargs: Any) -> Any:
        return self._value

    async def get_store(self, **kwargs: Any) -> Any:
        return self._value


class _ProviderSettings:
    llm = "fake"
    checkpoint = "redis"
    checkpoint_conn_string = None
    # Keep-forever retention, so the park-persist expiry-vs-retention gate bounds nothing
    # and this resume-mechanics test's synthetic None-expiry park passes it.
    checkpoint_ttl_minutes = None
    store = "memory"
    store_conn_string = None


class _LlmSettings:
    def with_fallbacks(self, kwargs: Any) -> dict[str, Any]:
        return {}


def _wire(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(base_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(base_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(base_mod, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(base_mod, "llm_settings", _LlmSettings)
    monkeypatch.setattr(base_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(tools_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(drv, "llm_provider_settings", _ProviderSettings)


def _agent() -> tools_mod.ToolsAgent:
    agent = tai42_app.agents.get_agent("tools_agent")
    assert isinstance(agent, tools_mod.ToolsAgent)
    return agent


async def _park_via_astream(agent: tools_mod.ToolsAgent, thread_id: str) -> None:
    """Drive a fresh turn through the astream face with the completion tool bound, so the
    run parks with a completion delivery path recorded on its park entry."""
    token = set_park_completion(_COMPLETION_TOOL, _completion_context(thread_id))
    try:
        async for _event in agent.astream(
            tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id=thread_id
        ):
            pass
    finally:
        reset_park_completion(token)


def _expected_delivery(thread_id: str, interaction_ids: list[str], result: Any) -> dict[str, Any]:
    """The exact kwargs a clean-terminal completion handoff fires: the bound opaque context, the
    final answer, the stable completion id derived from (thread_id, super-step of the resolved
    interactions), and the succeeded terminal status."""
    completion_id = drv._completion_id(thread_id, idx.compute_superstep_id(interaction_ids))
    return {
        **_completion_context(thread_id),
        "result": result,
        "completion_id": completion_id,
        "status": PARK_COMPLETION_SUCCEEDED,
    }


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


def _expected_failed_delivery(thread_id: str, interaction_ids: list[str]) -> dict[str, Any]:
    """The exact kwargs the abandonment fire dispatches: the bound opaque context, a ``None``
    result (nothing was produced), the SAME stable completion id a clean terminal would use, and
    the FAILED terminal status."""
    completion_id = drv._completion_id(thread_id, idx.compute_superstep_id(interaction_ids))
    return {
        **_completion_context(thread_id),
        "result": None,
        "completion_id": completion_id,
        "status": PARK_COMPLETION_FAILED,
    }


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

        await drv.fire_park_failed_completion("i1")
        assert delivered == [_expected_failed_delivery("bridge:acme:alice", ["i1"])]

        # A second abandonment fire (e.g. a sibling's redelivery, or a re-run reaper pass) re-fires
        # under the SAME completion id, so a delivery ledger keyed on that id collapses it to one
        # record — the driver does not tombstone; the id is the exactly-once authority.
        await drv.fire_park_failed_completion("i1")
        assert [d["completion_id"] for d in delivered] == [
            drv._completion_id("bridge:acme:alice", idx.compute_superstep_id(["i1"])),
            drv._completion_id("bridge:acme:alice", idx.compute_superstep_id(["i1"])),
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
        await drv.fire_park_failed_completion("i1")
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
        await drv.fire_park_failed_completion("nonexistent")
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
        await drv.fire_park_failed_completion("nested-i1")
        assert delivered == [
            {
                "chain_token": chain_token,
                "result": None,
                "completion_id": drv._completion_id(thread_id, idx.compute_superstep_id(["nested-i1"])),
                "status": PARK_COMPLETION_FAILED,
            }
        ]

    asyncio.run(go())


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

        await drv.fire_park_failed_completion("i1")
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
        await drv.fire_park_failed_completion("nested-i1")
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
        await drv.fire_park_failed_completion("i1")
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
        await drv.fire_park_failed_completion("i1")
        assert delivered == []

        # The drive released without resolving (its own error path): abandonment now fires.
        await idx.release_claim("bridge:acme:alice", superstep_id, "other-drive")
        await drv.fire_park_failed_completion("i1")
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
        await drv.fire_park_failed_completion("i1")
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
    assert drv.fire_park_failed_completion in before
    register_agent_resume_tool()
    assert list(cont._continuation_abandonment_handlers) == before


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
                tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="bridge:acme:alice"
            ):
                pass
        finally:
            reset_park_completion(token)
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert entry["completion_context"] is None

        assert await agent_resume("i1", "the answer") == "all done"
        completion_id = drv._completion_id("bridge:acme:alice", idx.compute_superstep_id(["i1"]))
        assert delivered == [
            {"result": "all done", "completion_id": completion_id, "status": PARK_COMPLETION_SUCCEEDED}
        ]

    asyncio.run(go())


def test_completion_context_falls_back_to_the_parked_thread_on_a_fieldless_entry() -> None:
    # A stored entry predating the field carries no ``completion_context`` at all: the fallback
    # reproduces exactly what the old driver injected, so an in-flight AGENT-route park keeps its
    # address instead of dropping its answer.
    assert drv._completion_context({"thread_id": "bridge:acme:alice"}, "bridge:acme:alice") == {
        "thread_id": "bridge:acme:alice"
    }
    # PRESENCE is what is tested, so a field that IS present is authoritative — including an
    # explicit ``None`` (the binder wired no routing context), which never resurrects the
    # thread fallback.
    assert drv._completion_context({"completion_context": {"delivery_thread_id": "t"}}, "th") == {
        "delivery_thread_id": "t"
    }
    assert drv._completion_context({"completion_context": None}, "th") is None


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


def _wire_deep(
    monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver, store: InMemoryStore
) -> None:
    """Keep the REAL build_langchain_deep_agent but inject the scripted model + a SHARED
    checkpointer/store, so a langchain_deep_agent park run and its resume read one durable checkpoint."""

    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(agent_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(agent_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(agent_mod, "store_registry", lambda: _Registry(store))
    monkeypatch.setattr(agent_mod, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(agent_mod, "llm_settings", _LlmSettings)
    monkeypatch.setattr(agent_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(drv, "llm_provider_settings", _ProviderSettings)


async def _park_via_astream_deep(agent: Any, thread_id: str) -> None:
    """Drive a fresh langchain_deep_agent turn through the astream face with the completion tool bound,
    so the run parks with a completion delivery path recorded on its park entry."""
    token = set_park_completion(_COMPLETION_TOOL, _completion_context(thread_id))
    try:
        async for _event in agent.astream(
            tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id=thread_id
        ):
            pass
    finally:
        reset_park_completion(token)


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
            drv._completion_id("bridge:acme:alice", superstep_id),
            drv._completion_id("bridge:acme:alice", superstep_id),
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
            drv._completion_id("bridge:acme:alice", superstep_id),
            drv._completion_id("bridge:acme:alice", superstep_id),
        ]
        # The successful drive finalized the entry to a resolved tombstone.
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())
