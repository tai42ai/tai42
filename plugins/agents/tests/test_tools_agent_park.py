"""The ``tools_agent`` async ``ask_user`` park/resume — parity with ``langchain_deep_agent``.

``tools_agent`` builds through ``create_agent`` (not ``create_deep_agent``); these tests
prove the SAME shared park machinery covers it: the ``run`` face parks on an async ask and
returns a suspended receipt, ``agent_resume`` rebuilds the graph on a fresh runtime (same
checkpointer + the durable park index) and drives it to completion running the parked tool
exactly once, expiry feeds the expiry marker, a non-durable/non-rebuildable run refuses,
a park raised by a NESTED run (which resumes on its own path, never through this loop) is
refused to the model instead of adopted, and the ``astream`` face (no completion delivery
bound) refuses an async ask loudly.

The park index is backed by an in-memory fakeredis routed through the index module's
``client_ctx`` seam; the checkpoint is an ``InMemorySaver`` shared between the park run and
the resume, standing in for the durable checkpoint a cross-worker resume reads.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import PrivateAttr
from tai42_contract.agent.base import PresetSpec
from tai42_contract.agent.events import SuspendedFinal
from tai42_contract.app import tai42_app
from tai42_contract.interactions import (
    EXPIRY_ANSWER,
    SUSPENDED_INTERACTION_MARKER_KEY,
    SuspendedInteraction,
    get_resume_continuation_tool,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
    suspended_interaction_marker,
)

from tai42_agents import tools_agent as tools_mod
from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import agent_resume
from tai42_agents._internal.park import driver as drv
from tai42_agents._internal.park import index as idx
from tai42_agents._internal.park.driver import AGENT_RESUME_TOOL_NAME


class ScriptedChatModel(BaseChatModel):
    _responses: list[BaseMessage] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)
    # Every prompt the loop fed the model, so a test can assert what the MODEL saw of a
    # tool outcome (a refusal has to reach the model to be answerable in the same turn).
    _seen: list[list[BaseMessage]] = PrivateAttr(default_factory=list)

    def __init__(self, responses: Sequence[BaseMessage], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    @property
    def seen(self) -> list[list[BaseMessage]]:
        return self._seen

    def bind_tools(self, tools: Any, **kwargs: Any) -> ScriptedChatModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        self._seen.append(list(messages))
        message = self._responses[self._index]
        self._index += 1
        return ChatResult(generations=[ChatGeneration(message=message)])


class _AskStandIn:
    """A faithful ``ask_user(mode="async")`` stand-in: it parks (returns the reserved
    marker) ONLY when a resume continuation is bound, and otherwise RAISES exactly as the
    platform helper does with no driver bound — so a face that binds no resume path refuses
    loudly. Counts calls so a resume that re-ran the tool (a double-park) is caught."""

    def __init__(self, interaction_id: str, expiry_at: Any = None) -> None:
        self.calls = 0
        self._interaction_id = interaction_id
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            self.calls += 1
            if get_resume_continuation_tool() is None:
                raise RuntimeError("async ask requires a resuming driver (no resume_continuation_tool is bound)")
            return suspended_interaction_marker(self._interaction_id, self._expiry_at, get_resume_continuation_tool())

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _ask_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "ask", "args": {}}])


class _NestedDriverStandIn:
    """A base tool that runs a NESTED DRIVER which async-parks — a flow preset, another
    agent run — the shape the agent loop cannot resume.

    The nested driver binds its own resume continuation for the ask, so the platform stamps
    THAT continuation onto the parked interaction and drives the resume there; the receipt
    coming back names it as the park's ``resume_owner``. Counts calls so a test can assert the
    nested run happened (its park is real and untouched) even though the agent refused it."""

    def __init__(self, interaction_id: str, resume_owner: str | None) -> None:
        self.calls = 0
        self._interaction_id = interaction_id
        self._resume_owner = resume_owner

    def run(self, **_kwargs: Any) -> SuspendedInteraction:
        self.calls += 1
        return SuspendedInteraction(interaction_id=self._interaction_id, resume_owner=self._resume_owner)

    def base_tool(self) -> StructuredTool:
        def flow(marker: str = "") -> str:
            """Run the nested flow."""
            return marker

        return StructuredTool.from_function(flow, name="flow", description="Run the nested flow.")


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
    monkeypatch.setattr(drv, "agents_park_redis_settings", lambda: settings)
    return redis


class _Registry:
    def __init__(self, value: Any) -> None:
        self._value = value

    async def get_checkpointer(self, **kwargs: Any) -> Any:
        return self._value


class _ProviderSettings:
    llm = "fake"
    checkpoint = "redis"
    checkpoint_conn_string = None
    # Keep-forever retention, so the park-persist expiry-vs-retention gate bounds nothing
    # and these resume-mechanics tests' synthetic None-expiry parks pass it.
    checkpoint_ttl_minutes = None
    store = "memory"
    store_conn_string = None


class _LlmSettings:
    def with_fallbacks(self, kwargs: Any) -> dict[str, Any]:
        return {}


def _wire_tools_build(monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver) -> None:
    """Keep the REAL ``_compile_tools_agent`` but inject the scripted model + a SHARED
    checkpointer, so the park run and its resume read one durable checkpoint. The
    monitoring-callback wiring is stripped from the run config (the recording writer hands
    back string sentinels the graph cannot use)."""

    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(base_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(base_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(base_mod, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(base_mod, "llm_settings", _LlmSettings)
    monkeypatch.setattr(base_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    monkeypatch.setattr(tools_mod, "init_langgraph_config", lambda config=None: dict(config or {}))
    # build_park_identity resolves the checkpoint provider through the kit settings when the
    # caller passes none; here the caller pins "redis", but pin the driver's view too so the
    # durable-provider gate is deterministic.
    monkeypatch.setattr(drv, "llm_provider_settings", _ProviderSettings)


def _agent() -> tools_mod.ToolsAgent:
    agent = tai42_app.agents.get_agent("tools_agent")
    assert isinstance(agent, tools_mod.ToolsAgent)
    return agent


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
            tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-tools"
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


def test_tools_agent_resume_delivers_a_legacy_ownerless_park_answer_end_to_end(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # F3/G1 PRODUCTION WIRING (end-to-end): a park written by a release predecessor carries no
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
            tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-legacy-e2e"
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
            tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-expiry"
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
        await agent.run(tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-hitl")
        entry = await idx.read_park_entry("i1")
        # Exactly one park interrupt id was stored — no second (HITL) interrupt exists.
        assert entry is not None
        assert isinstance(entry["interrupt_id"], str)
        result = await agent_resume("i1", "answer")
        assert result == "done"

    asyncio.run(go())


def _preset_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "flow_preset", "args": {}}])


def _tool_messages(prompt: list[Any]) -> list[Any]:
    return [message for message in prompt if isinstance(message, ToolMessage)]


@pytest.mark.parametrize(
    ("resume_owner", "owner_in_message"),
    [
        # A nested DRIVER (a flow preset) binds its own continuation for the ask.
        ("nested_driver_resume", "raised under a different run's resume binding"),
        # A nested agent RUN parked at its tool face: the receipt names no adoptable owner.
        (None, "no adoptable owner"),
    ],
)
def test_tools_agent_refuses_to_adopt_a_nested_runs_park(
    fake_park_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    app_tools: Any,
    resume_owner: str | None,
    owner_in_message: str,
) -> None:
    # A tool that drives a NESTED run which async-parks hands back a park the platform
    # resumes on that run's own path — ``agent_resume`` is never fired for it. Adopting it
    # would suspend this turn forever, so the loop refuses: the refusal reaches the MODEL as
    # a tool error, the turn finishes on the model's own next message, and this run records
    # no park state at all.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", resume_owner)
    model = ScriptedChatModel([_preset_call(), AIMessage(content="that flow needs its own route")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["flow"] = nested.base_tool()
    app_tools.tool_runners["flow"] = nested.run

    agent = _agent()
    preset = PresetSpec(name="flow_preset", base_tool="flow", description="Run the nested flow.", fixed_kwargs={})

    async def go() -> None:
        result = await agent.run(presets=[preset], checkpoint_provider="redis", user_message="go", thread_id="t-nested")
        # The turn COMPLETED on the refusal — not a park receipt, not a raised run.
        assert result == "that flow needs its own route"
        assert nested.calls == 1
        # The refusal is model-visible: the second prompt carries it as the tool's result, so
        # the model can answer around it in this turn.
        refusals = _tool_messages(model.seen[1])
        assert len(refusals) == 1
        assert refusals[0].status == "error"
        assert owner_in_message in refusals[0].content
        # The refusal never NAMES the expected owner: echoing the bound continuation would hand a
        # model the exact string to name for a claim, so the message stays store-name-free.
        assert "agent_resume" not in refusals[0].content
        # Nor does it echo the park's own claimed owner value back.
        if resume_owner is not None:
            assert repr(resume_owner) not in refusals[0].content
        # Nothing of THIS run is parked or orphaned: no park entry was written for the nested
        # interaction (the nested run's own park is untouched and resumes on its own path).
        assert await idx.read_park_entry("i-nested") is None

    asyncio.run(go())


def test_park_continuation_binds_none_when_not_park_capable_shadowing_the_ambient() -> None:
    # F1: a non-park-capable run's drive wrapper binds ``None``, SHADOWING any ambient resume
    # continuation a park-capable caller left bound. Without it, a nested non-capable run inherits
    # the ambient binding, its ask mints a park it can never resume, the claim point adopts it, and
    # the run answers with the raw marker (or the stream ends with no terminal).
    outer = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
    try:
        assert get_resume_continuation_tool() == AGENT_RESUME_TOOL_NAME
        with drv.park_continuation(None):
            # The ambient binding is shadowed for the duration of the non-capable drive.
            assert get_resume_continuation_tool() is None
        # And restored when the wrapper exits.
        assert get_resume_continuation_tool() == AGENT_RESUME_TOOL_NAME
    finally:
        reset_resume_continuation_tool(outer)


def test_tools_agent_under_ambient_binding_does_not_answer_with_a_marker_when_not_hostable(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # F1 (probe shape, run face): a memory-checkpoint run is not park-capable, so its drive wrapper
    # binds None even under an ambient resume continuation a park-capable caller left bound. A
    # relayed marker naming that ambient continuation is then REFUSED at the claim point rather than
    # adopted, so the run never answers with the marker JSON.
    saver = InMemorySaver()
    relay = _RawMarkerTool(suspended_interaction_marker("i-relayed", None, AGENT_RESUME_TOOL_NAME))
    model = ScriptedChatModel([_relay_call(), AIMessage(content="cannot host that here")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["relay"] = relay.tool()

    agent = _agent()

    async def go() -> None:
        # Stand in for being nested under a park-capable caller that bound the resume continuation.
        outer = set_resume_continuation_tool(AGENT_RESUME_TOOL_NAME)
        try:
            result = await agent.run(
                tool_names=["relay"], checkpoint_provider="memory", user_message="go", thread_id="t-f1"
            )
        finally:
            reset_resume_continuation_tool(outer)
        assert isinstance(result, str)
        assert SUSPENDED_INTERACTION_MARKER_KEY not in result
        assert "i-relayed" not in result
        assert result == "cannot host that here"
        # Nothing was parked under this non-hostable run.
        assert await idx.read_park_entry("i-relayed") is None

    asyncio.run(go())


def test_tools_agent_run_refuses_non_durable_checkpoint(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A memory checkpoint is not park-capable: the async ask has no resume path bound, so the
    # ask stand-in RAISES loudly pre-persist rather than parking with nowhere to resume.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        with pytest.raises(Exception, match="resuming driver"):
            await agent.run(
                tool_names=["ask"], checkpoint_provider="memory", user_message="go", thread_id="t-nondurable"
            )
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())


def test_tools_agent_run_refuses_live_tools(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # A run carrying a LIVE tool is not rebuildable from names, so it is not park-capable:
    # the resume continuation is not bound and the async ask refuses loudly pre-persist.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)

    agent = _agent()

    async def go() -> None:
        with pytest.raises(Exception, match="resuming driver"):
            await agent.run(tools=[ask.tool()], checkpoint_provider="redis", user_message="go", thread_id="t-livetools")
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())


def test_tools_agent_astream_refuses_async_ask_without_completion(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The astream face binds NO resume path when no completion tool is bound (a direct SSE
    # run), so an async ask refuses loudly pre-persist — the option-ii boundary is unchanged.
    saver = InMemorySaver()
    ask = _AskStandIn("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["ask"] = ask.tool()

    agent = _agent()

    async def go() -> None:
        with pytest.raises(Exception, match="resuming driver"):
            async for _event in agent.astream(
                tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-astream"
            ):
                pass
        assert await idx.read_park_entry("i1") is None

    asyncio.run(go())


class _RawMarkerTool:
    """A tool whose RESULT is a park marker this run never minted — the wire form, as a
    non-park-capable middle agent hands it on, as an older wire form carries it, or as a
    model could shape it. No sentinel object ever crosses a tool face here, so only the
    CLAIM point can catch it."""

    def __init__(self, marker: dict[str, Any]) -> None:
        self.calls = 0
        self._marker = marker

    def tool(self) -> StructuredTool:
        def relay() -> dict[str, Any]:
            self.calls += 1
            return self._marker

        return StructuredTool.from_function(relay, name="relay", description="Relay a nested result.")


def _relay_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "relay", "args": {}}])


@pytest.mark.parametrize(
    ("marker", "why"),
    [
        # A nested driver's park, relayed as content by a middle agent that could not park.
        (suspended_interaction_marker("i-relayed", None, "nested_driver_resume"), "different run's resume binding"),
        # An older wire form / a nested RUN's park: no owner rides it at all.
        ({SUSPENDED_INTERACTION_MARKER_KEY: {"interaction_id": "i-relayed", "expiry_at": None}}, "no adoptable owner"),
        # Content a MODEL shaped to look like a park, reserved key and all.
        (
            {SUSPENDED_INTERACTION_MARKER_KEY: {"interaction_id": "i-relayed", "expiry_at": None, "resume_owner": ""}},
            "no adoptable owner",
        ),
    ],
    ids=["foreign", "ownerless", "injected"],
)
def test_tools_agent_refuses_to_claim_a_marker_it_does_not_own(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any, marker: dict[str, Any], why: str
) -> None:
    # THE CLAIM POINT. The park arrives as tool-result CONTENT — no sentinel object crossed a
    # seam — so the object-seam guards were never on this path. Claiming it would write this
    # run's park entry over the owning run's, then wait for a resume fired only at that run.
    saver = InMemorySaver()
    relay = _RawMarkerTool(marker)
    model = ScriptedChatModel([_relay_call(), AIMessage(content="I cannot run that here")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["relay"] = relay.tool()

    agent = _agent()

    async def go() -> None:
        result = await agent.run(
            tool_names=["relay"], checkpoint_provider="redis", user_message="go", thread_id="t-claim"
        )
        # The turn COMPLETED on the refusal: not a park receipt, not a raised run.
        assert result == "I cannot run that here"
        assert relay.calls == 1
        # The refusal reached the MODEL as this tool call's error result — never the raw
        # marker JSON as if it were the tool's answer, and never a dropped tool_call.
        refusals = _tool_messages(model.seen[1])
        assert len(refusals) == 1
        assert refusals[0].status == "error"
        assert why in refusals[0].content
        assert SUSPENDED_INTERACTION_MARKER_KEY not in refusals[0].content
        # The refusal never names the bound resume continuation (the forgery string).
        assert "agent_resume" not in refusals[0].content
        # Nothing of the relayed park was claimed: no index entry under this run.
        assert await idx.read_park_entry("i-relayed") is None

    asyncio.run(go())


def test_tools_agent_run_never_answers_with_a_park_marker(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Probe shape (run face): a park this run cannot host must never escape as the run's
    # ANSWER — the marker's JSON is a suspend SIGNAL, and reading as text it would be an
    # answer no one wrote.
    saver = InMemorySaver()
    relay = _RawMarkerTool(suspended_interaction_marker("i-relayed", None, "nested_driver_resume"))
    model = ScriptedChatModel([_relay_call(), AIMessage(content="done without it")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["relay"] = relay.tool()

    agent = _agent()

    async def go() -> None:
        result = await agent.run(
            tool_names=["relay"], checkpoint_provider="redis", user_message="go", thread_id="t-escape"
        )
        assert isinstance(result, str)
        assert SUSPENDED_INTERACTION_MARKER_KEY not in result
        assert "i-relayed" not in result
        assert result == "done without it"

    asyncio.run(go())


def test_tools_agent_astream_still_reaches_a_terminal_event(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Probe shape (streaming face): a park this run cannot host must not truncate the stream.
    # A drive that stops on an interrupt nothing classifies yields no terminal event, and a
    # consumer waiting for one waits forever.
    saver = InMemorySaver()
    relay = _RawMarkerTool(suspended_interaction_marker("i-relayed", None, "nested_driver_resume"))
    model = ScriptedChatModel([_relay_call(), AIMessage(content="done without it")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["relay"] = relay.tool()

    agent = _agent()

    async def go() -> None:
        events = [
            event
            async for event in agent.astream(
                tool_names=["relay"], checkpoint_provider="redis", user_message="go", thread_id="t-stream"
            )
        ]
        assert [event for event in events if getattr(event, "final", False)], events
        assert not [event for event in events if isinstance(event, SuspendedFinal)]

    asyncio.run(go())


def _park() -> drv.ParkIdentity:
    return drv.ParkIdentity(agent_name="a", thread_id="t", rebuild_kwargs={}, bind=True)


def test_bind_resume_per_step_scopes_the_binding_to_the_step_not_the_consumer() -> None:
    # F4: the resume continuation must be bound WHILE a drive step is computed, but never leak
    # across the yield into the consumer. PEP 568 is unimplemented, so a ``with`` in the yielding
    # generator's own body would land the ContextVar in the CONSUMER's task and persist it across
    # every yield. ``bind_resume_per_step`` re-enters the binding per ``__anext__`` instead.
    park = _park()
    seen_in_step: list[str | None] = []

    async def inner() -> Any:
        for i in range(3):
            # Inside __anext__ (the step): the binding is active, so the tool dispatch sees it.
            seen_in_step.append(get_resume_continuation_tool())
            yield i

    async def go() -> list[str | None]:
        seen_in_consumer: list[str | None] = []
        async for _item in drv.bind_resume_per_step(lambda: drv.park_continuation(park), inner()):
            # In the consumer, between steps, the binding must NOT be visible.
            seen_in_consumer.append(get_resume_continuation_tool())
        return seen_in_consumer

    assert get_resume_continuation_tool() is None
    seen_in_consumer = asyncio.run(go())
    assert seen_in_step == [AGENT_RESUME_TOOL_NAME] * 3
    assert seen_in_consumer == [None, None, None]
    # No binding leaked out after the stream drained.
    assert get_resume_continuation_tool() is None


def test_bind_resume_per_step_abandoned_midstream_leaves_a_clean_context() -> None:
    # F4: abandoning the stream mid-flight must close the inner generator and leave the consumer's
    # context clean — no binding stranded, and no ValueError from a foreign-context Token reset on
    # aclose (the failure the in-generator ``with`` produced).
    park = _park()
    closed = False

    async def inner() -> Any:
        nonlocal closed
        try:
            i = 0
            while True:
                yield i
                i += 1
        finally:
            closed = True

    async def go() -> None:
        agen = drv.bind_resume_per_step(lambda: drv.park_continuation(park), inner())
        first = await agen.__anext__()
        assert first == 0
        # Consumer context is clean the moment the item is handed over.
        assert get_resume_continuation_tool() is None
        # Abandon mid-stream: aclose must not raise and must leave the context clean.
        await agen.aclose()

    asyncio.run(go())
    assert closed is True
    assert get_resume_continuation_tool() is None


def test_binding_inside_a_generator_body_leaks_into_the_consumer() -> None:
    # The anti-pattern ``bind_resume_per_step`` exists to replace: a ``with park_continuation``
    # wrapping the ``yield`` lands the ContextVar in the CONSUMER's task and persists it across the
    # yield. This pins the leak so the fix is not silently reverted to the in-generator form.
    park = _park()

    async def leaky() -> Any:
        with drv.park_continuation(park):
            for i in range(2):
                yield i

    async def go() -> list[str | None]:
        seen_in_consumer: list[str | None] = []
        async for _item in leaky():
            seen_in_consumer.append(get_resume_continuation_tool())
        return seen_in_consumer

    seen_in_consumer = asyncio.run(go())
    # The binding leaked: the consumer saw it bound across the yield (the defect).
    assert seen_in_consumer == [AGENT_RESUME_TOOL_NAME, AGENT_RESUME_TOOL_NAME]
