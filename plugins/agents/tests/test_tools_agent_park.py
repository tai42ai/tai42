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
import logging
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
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
    CHAINED_PARK_TOKEN_KEY,
    EXPIRY_ANSWER,
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_REPARKED,
    PARK_COMPLETION_SUCCEEDED,
    SUSPENDED_INTERACTION_MARKER_KEY,
    SuspendedInteraction,
    chained_park_context,
    get_park_completion,
    get_resume_continuation_tool,
    is_chained_park_key,
    reset_resume_continuation_tool,
    set_resume_continuation_tool,
    suspended_interaction_marker,
)

from tai42_agents import tools_agent as tools_mod
from tai42_agents._internal import base_tool_agent as base_mod
from tai42_agents._internal.park import agent_resume, deliver_chained_park
from tai42_agents._internal.park import driver as drv
from tai42_agents._internal.park import index as idx
from tai42_agents._internal.park.driver import AGENT_RESUME_TOOL_NAME
from tai42_agents._internal.park.errors import AgentResumeParkEntryNotFoundError


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
    """A base tool that runs a NESTED DRIVER which async-parks — a nested-driver preset, another
    agent run — the shape the agent loop cannot resume through its own continuation.

    The nested driver binds its own resume continuation for the ask, so the platform stamps
    THAT continuation onto the parked interaction and drives the resume there; the receipt
    coming back names it as the park's ``resume_owner``. Counts calls so a test can assert the
    nested run happened (its park is real and untouched), and records the chained key it saw
    bound around the dispatch — which is the address its driver would later fire its terminal
    at, and empty when the caller chained nothing."""

    def __init__(self, interaction_id: str, resume_owner: str | None, expiry_at: datetime | None = None) -> None:
        self.calls = 0
        self.chained_keys: list[str] = []
        self._interaction_id = interaction_id
        self._resume_owner = resume_owner
        self._expiry_at = expiry_at

    def run(self, **_kwargs: Any) -> SuspendedInteraction:
        self.calls += 1
        _tool, context = get_park_completion()
        if context is not None and CHAINED_PARK_TOKEN_KEY in context:
            self.chained_keys.append(context[CHAINED_PARK_TOKEN_KEY])
        return SuspendedInteraction(
            interaction_id=self._interaction_id, resume_owner=self._resume_owner, expiry_at=self._expiry_at
        )

    def base_tool(self) -> StructuredTool:
        def nested_driver(marker: str = "") -> str:
            """Run the nested driver."""
            return marker

        return StructuredTool.from_function(nested_driver, name="nested_driver", description="Run the nested driver.")


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
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "driver_preset", "args": {}}])


def _tool_messages(prompt: list[Any]) -> list[Any]:
    return [message for message in prompt if isinstance(message, ToolMessage)]


@pytest.mark.parametrize(
    "resume_owner",
    [
        # A nested DRIVER (a nested-driver preset) binds its own continuation for the ask.
        "nested_driver_resume",
        # A nested agent RUN parked at its tool face: the receipt names no adoptable owner.
        None,
    ],
    ids=[
        "a nested driver raised under a different run's resume binding",
        "a nested run's tool face, no adoptable owner",
    ],
)
def test_tools_agent_chains_a_nested_runs_park_and_resumes_with_its_terminal(
    fake_park_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    app_tools: Any,
    resume_owner: str | None,
) -> None:
    # THE HEADLINE. A tool that drives a NESTED run which async-parks hands back a park the
    # platform resumes on that run's own path — ``agent_resume`` is never fired for it. The turn
    # neither adopts it nor gives up on it: it parks on the CALL, and when the nested run finally
    # reaches its terminal, the chain's delivery tool re-enters the loop with that terminal as
    # the tool's result and the turn finishes on it.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", resume_owner)
    model = ScriptedChatModel([_preset_call(), AIMessage(content="the call approved it")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset], checkpoint_provider="redis", user_message="go", thread_id="t-chained"
        )
        assert isinstance(receipt, dict)
        assert receipt["status"] == "suspended"
        # Parked on the CALL, not on the nested run's interaction: that park stays untouched and
        # resumes on its own path.
        (chain_token,) = receipt["interaction_ids"]
        assert is_chained_park_key(chain_token)
        assert nested.calls == 1
        assert await idx.read_park_entry("i-nested") is None
        entry = await idx.read_park_entry(chain_token)
        assert entry is not None
        assert entry["agent_name"] == "tools_agent"

        # The nested run reaches its terminal out of band and fires the chain's delivery tool
        # with the contract-generic completion payload.
        assert (
            await deliver_chained_park(
                chain_token=chain_token,
                result={"decision": "approved"},
                completion_id="c-1",
                status=PARK_COMPLETION_SUCCEEDED,
            )
            == "the call approved it"
        )
        # The model read the nested run's terminal AS the tool's result — a success result, not
        # an error — and answered on it.
        results = _tool_messages(model.seen[1])
        assert len(results) == 1
        assert results[0].status == "success"
        assert "approved" in results[0].content
        # The chained park finalizes like any other: a resolved tombstone, never an absent key.
        entry = await idx.read_park_entry(chain_token)
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_two_chained_calls_in_one_super_step_resume_together(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Two parking calls in ONE super-step are two CALLS, so two chained keys — never one park
    # both terminals would land on. They converge on the same barrier the platform's own
    # siblings use: the first terminal buffers, and only the second drives the turn on.
    saver = InMemorySaver()
    first = _NestedDriverStandIn("i-a", "nested_driver_resume")
    second = _NestedDriverStandIn("i-b", "nested_driver_resume")
    model = ScriptedChatModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "c1", "name": "driver_a", "args": {}},
                    {"id": "c2", "name": "driver_b", "args": {}},
                ],
            ),
            AIMessage(content="both calls are in"),
        ]
    )
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["a"] = first.base_tool()
    app_tools.client_tools["b"] = second.base_tool()
    app_tools.tool_runners["a"] = first.run
    app_tools.tool_runners["b"] = second.run

    agent = _agent()
    presets = [
        PresetSpec(name="driver_a", base_tool="a", description="Run driver a.", fixed_kwargs={}),
        PresetSpec(name="driver_b", base_tool="b", description="Run driver b.", fixed_kwargs={}),
    ]

    async def go() -> None:
        receipt = await agent.run(presets=presets, checkpoint_provider="redis", user_message="go", thread_id="t-two")
        keys = receipt["interaction_ids"]
        assert len(set(keys)) == 2
        assert set(keys) == {first.chained_keys[0], second.chained_keys[0]}

        buffered = await deliver_chained_park(
            chain_token=keys[0], result="a done", completion_id="c-a", status=PARK_COMPLETION_SUCCEEDED
        )
        assert buffered == {"status": "buffered", "remaining": 1}
        assert (
            await deliver_chained_park(
                chain_token=keys[1], result="b done", completion_id="c-b", status=PARK_COMPLETION_SUCCEEDED
            )
            == "both calls are in"
        )
        # Each call's own terminal became its own tool result.
        results = sorted(message.content for message in _tool_messages(model.seen[1]))
        assert results == ["a done", "b done"]

    asyncio.run(go())


def test_the_delivery_tool_accepts_the_whole_contract_fire_payload(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # What a nested driver actually fires is the BOUND CONTEXT merged with the terminal —
    # every field of it, not just the ones this tool reads. The context a chained dispatch
    # composes carries the embedded caller binding and (under a tool route) the reserved thread
    # field, so a fire carrying them must not be a signature error: that failure mode is a turn
    # that parks, resumes nowhere, and is retried until it is dropped.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="the call approved it")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset], checkpoint_provider="redis", user_message="go", thread_id="t-payload"
        )
        (chain_token,) = receipt["interaction_ids"]
        fire = {
            **chained_park_context(chain_token, ("deliver_tool_completion", {"delivery_thread_id": "bridge:r:a"})),
            "result": "the call said yes",
            "completion_id": "c-1",
            "status": PARK_COMPLETION_SUCCEEDED,
        }
        assert await deliver_chained_park(**fire) == "the call approved it"

    asyncio.run(go())


def test_a_chained_park_inherits_the_nested_asks_horizon(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The caller's suspension should last exactly as long as the thing it waits on, so the
    # chained park's deadline is INHERITED from the ask the nested run is parked on.
    saver = InMemorySaver()
    deadline = datetime.now(UTC) + timedelta(hours=2)
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume", expiry_at=deadline)
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset], checkpoint_provider="redis", user_message="go", thread_id="t-horizon"
        )
        assert receipt["expiry_at"] == deadline.isoformat()

    asyncio.run(go())


def test_a_chained_park_horizon_is_capped(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # Inheritance is not blind trust: a chained park has no ask of its own and nothing fires at
    # its deadline, so a nested run asking for an answer years away cannot pin a suspended agent
    # open that long. The cap is what bounds it, and a nested run carrying NO deadline takes the
    # cap too — a chained park is never unbounded.
    saver = InMemorySaver()
    monkeypatch.setattr(drv, "agents_limits_settings", lambda: SimpleNamespace(chained_park_horizon_cap_hours=1))
    far = datetime.now(UTC) + timedelta(days=365)
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume", expiry_at=far)
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(presets=[preset], checkpoint_provider="redis", user_message="go", thread_id="t-cap")
        capped = datetime.fromisoformat(receipt["expiry_at"])
        assert capped < far
        assert capped <= datetime.now(UTC) + timedelta(hours=1)

    asyncio.run(go())


def test_a_chained_park_horizon_reads_a_naive_inherited_deadline_as_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    # An inherited deadline crosses the delivery boundary (deliver_chained_park's expiry_at) as an
    # ISO string. Internal producers stamp UTC, but a malformed external fire could omit the offset,
    # and a naive value compared against the tz-aware clamp raises TypeError. It must be read as UTC
    # instead: the horizon only sizes how long the index holds the park (nothing fires AT it), so
    # the "clamped, never refused" contract holds and a cosmetically-off deadline never becomes an
    # endless redelivery of the re-park notice.
    monkeypatch.setattr(drv, "agents_limits_settings", lambda: SimpleNamespace(chained_park_horizon_cap_hours=24))
    naive = (datetime.now(UTC) + timedelta(hours=1)).replace(tzinfo=None)
    horizon = drv.chained_park_horizon(naive.isoformat(), None)
    parsed = datetime.fromisoformat(horizon)
    assert parsed.tzinfo is not None
    # Read as UTC and, being inside the cap, kept as the inherited deadline.
    assert parsed == naive.replace(tzinfo=UTC)


def test_a_re_park_extends_the_chained_parks_inherited_horizon(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The nested run answered its first ask and asked another one, further out. The waiting
    # caller's inherited horizon moves with it — the platform fires the re-park notice at the
    # chained binding, and the delivery tool extends the park's keys rather than resuming it.
    saver = InMemorySaver()
    monkeypatch.setattr(drv, "agents_limits_settings", lambda: SimpleNamespace(chained_park_horizon_cap_hours=24 * 365))
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume", expiry_at=datetime.now(UTC) + timedelta(hours=1))
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset], checkpoint_provider="redis", user_message="go", thread_id="t-repark"
        )
        (chain_token,) = receipt["interaction_ids"]
        entry = await idx.read_park_entry(chain_token)
        assert entry is not None
        before = await fake_park_redis.ttl(f"agent:park:{chain_token}")

        # Past the park index's own backstop floor, so the extension is what sets the TTL.
        later = datetime.now(UTC) + timedelta(days=60)
        extended = await deliver_chained_park(
            chain_token=chain_token, status=PARK_COMPLETION_REPARKED, expiry_at=later.isoformat()
        )
        assert extended["status"] == "extended"
        # The park is still parked — a re-park notice resolves nothing — and now outlives the
        # NEW deadline. Its barrier moves with it, so the answer still finds both.
        assert await fake_park_redis.ttl(f"agent:park:{chain_token}") > before
        assert await fake_park_redis.ttl(f"agent:park:step:t-repark:{entry['superstep_id']}") > before
        assert not idx.is_resolved_tombstone(await idx.read_park_entry(chain_token) or {})

    asyncio.run(go())


def test_a_re_park_notice_for_an_unparked_chain_is_benign(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The FIRST park of a chained call always notifies before the waiting run has recorded its
    # own park (the nested run parks first, by construction), so a notice with nothing to extend
    # is the normal case, not an error.
    async def go() -> None:
        assert await deliver_chained_park(
            chain_token="tai42:chained-park:never-parked",
            status=PARK_COMPLETION_REPARKED,
            expiry_at=datetime.now(UTC).isoformat(),
        ) == {"status": "not_parked"}

    asyncio.run(go())


@pytest.mark.parametrize(
    ("status", "announced"),
    [
        # The explicit non-success terminal.
        (PARK_COMPLETION_FAILED, "non-success terminal"),
        # An UNSTAMPED fire: a driver that predates the status field, or one that omits it.
        (None, "carried NO status"),
        # A value outside the shared vocabulary — a driver/delivery version skew.
        ("mystery", "unrecognized status"),
    ],
)
def test_a_failed_nested_terminal_re_enters_as_a_model_visible_tool_error(
    fake_park_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
    app_tools: Any,
    caplog: pytest.LogCaptureFixture,
    status: str | None,
    announced: str,
) -> None:
    # The nested run ended without a result. The waiting turn is resumed either way — never left
    # parked — and what it is resumed WITH is a tool ERROR the model reads, never a silent empty
    # result and never a failure payload dressed as the answer.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="I could not run that call")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        receipt = await agent.run(
            presets=[preset], checkpoint_provider="redis", user_message="go", thread_id="t-failed"
        )
        (chain_token,) = receipt["interaction_ids"]
        with caplog.at_level(logging.WARNING):
            assert (
                await deliver_chained_park(chain_token=chain_token, completion_id="c-1", status=status)
                == "I could not run that call"
            )
        errors = _tool_messages(model.seen[1])
        assert len(errors) == 1
        assert errors[0].status == "error"
        assert "ended without a result" in errors[0].content
        # Every non-success shape is ANNOUNCED naming WHICH arrived: a fire this tool cannot read
        # still resumes the run, so the log is the only detection a version skew has.
        assert announced in caplog.text

    asyncio.run(go())


def test_a_terminal_for_a_chain_the_drive_never_parked_on_lands_benignly(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The turn claimed a chained call and then DIED without parking on it — here the drive raises
    # after the nested run parked. The nested run will still fire its terminal at that key, so
    # the drive detaches it on the way out: the late fire lands on the benign already-resolved
    # path instead of hunting a park that will never exist.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="unreached")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    async def _persist_died(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the drive died")

    monkeypatch.setattr(drv, "persist_superstep", _persist_died)

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        with pytest.raises(Exception, match="the drive died"):
            await agent.run(presets=[preset], checkpoint_provider="redis", user_message="go", thread_id="t-dead")
        (chain_token,) = nested.chained_keys
        entry = await idx.read_park_entry(chain_token)
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)
        # A late terminal reads the tombstone and clears, instead of raising into an endless
        # redelivery against an absent key.
        assert await deliver_chained_park(
            chain_token=chain_token, result="too late", completion_id="c-1", status=PARK_COMPLETION_SUCCEEDED
        ) == {"status": "already_resolved"}

    asyncio.run(go())


def test_a_terminal_for_a_chain_still_being_recorded_keeps_its_retry(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # The other side of the same coin: an ABSENT entry is not proof the caller is gone — it can
    # be a terminal that beat the waiting run's own park-persist. Raising keeps the platform's
    # at-least-once ticket so the redelivery lands, instead of dropping the answer.
    async def go() -> None:
        with pytest.raises(AgentResumeParkEntryNotFoundError):
            await deliver_chained_park(
                chain_token="tai42:chained-park:not-yet",
                result="the answer",
                completion_id="c-1",
                status=PARK_COMPLETION_SUCCEEDED,
            )

    asyncio.run(go())


def test_a_terminal_with_no_chain_token_is_dropped_loudly(caplog: pytest.LogCaptureFixture) -> None:
    # Unroutable: it names no waiting run and no retry could ever land it, so it is dropped with
    # an error rather than raising into an endless redelivery.
    async def go() -> None:
        with caplog.at_level(logging.ERROR):
            assert await deliver_chained_park(
                chain_token=None, result="orphan", completion_id="c-9", status=PARK_COMPLETION_SUCCEEDED
            ) == {"status": "dropped"}
        assert "no chain_token" in caplog.text

    asyncio.run(go())


def test_an_unchained_run_still_refuses_a_nested_runs_park(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    # THE FALLBACK, unchanged. A run that cannot park binds no resume continuation, so its
    # dispatches chain nothing — there would be nothing to deliver a nested terminal to. The
    # nested run's park is refused to the MODEL as a tool error, the turn finishes on the model's
    # own next message, and this run records no park state at all.
    saver = InMemorySaver()
    nested = _NestedDriverStandIn("i-nested", "nested_driver_resume")
    model = ScriptedChatModel([_preset_call(), AIMessage(content="that call needs its own route")])
    _wire_tools_build(monkeypatch, model, saver)
    app_tools.client_tools["nested_driver"] = nested.base_tool()
    app_tools.tool_runners["nested_driver"] = nested.run

    agent = _agent()
    preset = PresetSpec(
        name="driver_preset", base_tool="nested_driver", description="Run the nested driver.", fixed_kwargs={}
    )

    async def go() -> None:
        # The astream face with NO completion bound binds no resume path at all.
        events = [
            event
            async for event in agent.astream(
                presets=[preset], checkpoint_provider="redis", user_message="go", thread_id="t-unchained"
            )
        ]
        assert events
        assert nested.calls == 1
        refusals = _tool_messages(model.seen[1])
        assert len(refusals) == 1
        assert refusals[0].status == "error"
        # The refusal names the door that was shut — nothing chained this call — but stays
        # store-name-free: it never echoes the expected owner (a public continuation name would
        # hand the model the exact string to name for a claim). Both properties compose here.
        assert "raised under a different run's resume binding" in refusals[0].content
        assert "nothing chained this call" in refusals[0].content
        assert "agent_resume" not in refusals[0].content
        assert "'nested_driver_resume'" not in refusals[0].content
        # Nothing of THIS run is parked or orphaned, and no chained key was ever minted.
        assert await idx.read_park_entry("i-nested") is None
        assert nested.chained_keys == []

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
