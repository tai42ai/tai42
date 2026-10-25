"""A resumed agent run's terminal is settled under its routing's chain key before it reaches the caller.

The platform holds, per chain key, the output stages of the preset dispatches a chained call
crossed; the agents' resume drive settles each terminal through
``tai42_app.interactions.settle_chained_terminal`` under the key of the routing it reaches: the
fired routing for a chained terminal, a failed one included, and the routing the park captured
for a terminal a take-over returns inline. Two chained levels each settle under their own key.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    PARK_COMPLETION_SUCCEEDED,
    ChainedResume,
    RunFailed,
    reset_chained_resume,
    reset_resume_continuation_tool,
    set_chained_resume,
    set_resume_continuation_tool,
)
from tai42_kit.interactions.park_adoption import fire_terminal_chain
from tai42_kit.interactions.park_index import entry_ttl_seconds, superstep_id

from tai42_agents._internal.park.chain import CHAINED_PARK_DELIVERY_TOOL_NAME, deliver_chained_park
from tai42_agents._internal.park.park_binding import agents_park_index
from tai42_agents._internal.park.resume import agent_resume

from .conftest import APP

THREAD = "t-stages"
AGENT = "probe_staged_agent"
CALLER_KEY = "tai42:chained-park:caller"
INNER_KEY = "tai42:chained-park:inner"
RESUMER_KEY = "tai42:chained-park:resumer"


class _ResumableAgent:
    """A registered agent whose ``aresume_park`` records the answer and runs a scripted body."""

    def __init__(self) -> None:
        self.body: Callable[[], Any] = lambda: "resumed"
        self.answers: list[Any] = []

    async def aresume_park(self, *, rebuild_kwargs: Any, thread_id: str, resume_map: Any) -> Any:
        for answers in resume_map.values():
            self.answers.extend(answers.values())
        return await self.body()


class _ChainTool:
    """A chain-delivery tool: records each fire's payload and returns ``reply``."""

    def __init__(self) -> None:
        self.fired: list[dict[str, Any]] = []
        self.reply: Any = "the caller handled it"

    def __call__(self, **payload: Any) -> Any:
        self.fired.append(payload)
        return self.reply


class _Stages:
    """The platform's chained stages: ``mapped[key]`` wraps a success result settled under ``key``."""

    def __init__(self) -> None:
        self.settled: list[tuple[str, Any]] = []
        self.mapped: dict[str, str] = {}

    async def settle_chained_terminal(self, chain_key: str, result: Any) -> Any:
        self.settled.append((chain_key, result))
        if isinstance(result, RunFailed) or chain_key not in self.mapped:
            return result
        return {self.mapped[chain_key]: result}


@pytest.fixture
def agent() -> Any:
    probe = _ResumableAgent()
    APP.agents.registry[AGENT] = probe  # type: ignore[assignment]
    yield probe
    APP.agents.registry.pop(AGENT, None)


@pytest.fixture
def stages(monkeypatch: pytest.MonkeyPatch) -> _Stages:
    held = _Stages()
    monkeypatch.setattr(APP.interactions, "settle_chained_terminal", held.settle_chained_terminal, raising=False)
    return held


@pytest.fixture
def chain_tool(app_tools: Any) -> _ChainTool:
    tool = _ChainTool()
    app_tools.tool_runners["probe_chain_deliver"] = tool
    return tool


@pytest.fixture
def resumer_tool(app_tools: Any) -> _ChainTool:
    tool = _ChainTool()
    app_tools.tool_runners["probe_resumer_deliver"] = tool
    return tool


async def _park(interaction_id: str) -> None:
    """Park one super-step of the probe agent on ``interaction_id``, chained to the caller's routing."""
    step = superstep_id([interaction_id])
    await agents_park_index().persist(
        thread_id=THREAD,
        superstep=step,
        entries={
            interaction_id: {
                "agent_name": AGENT,
                "thread_id": THREAD,
                "superstep_id": step,
                "interrupt_id": "int-1",
                "rebuild_kwargs": {},
                "completion_tool": "probe_chain_deliver",
                "completion_context": {"chain_key": CALLER_KEY, "asked_by": ["caller"]},
            }
        },
        expected={interaction_id: None},
        entry_ttl={interaction_id: entry_ttl_seconds(None)},
        barrier_ttl=entry_ttl_seconds(None),
    )


def _returning(value: Any) -> Callable[[], Any]:
    async def body() -> Any:
        return value

    return body


def _raising(exc: BaseException) -> Callable[[], Any]:
    async def body() -> Any:
        raise exc

    return body


def test_a_chained_terminal_is_fired_as_the_stages_of_its_routing_settle_it(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool, stages: _Stages
) -> None:
    stages.mapped[CALLER_KEY] = "mapped"
    agent.body = _returning("the terminal")

    async def go() -> Any:
        await _park("i1")
        return await agent_resume("i1", "the answer")

    assert asyncio.run(go()) == "the caller handled it"
    assert stages.settled == [(CALLER_KEY, "the terminal")]
    assert chain_tool.fired == [
        {"chain_token": CALLER_KEY, "result": {"mapped": "the terminal"}, "status": PARK_COMPLETION_SUCCEEDED}
    ]


def test_a_failed_chained_terminal_is_fired_as_the_failure_the_stages_pass_through(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool, stages: _Stages
) -> None:
    stages.mapped[CALLER_KEY] = "mapped"
    agent.body = _raising(RuntimeError("provider down"))

    async def go() -> Any:
        await _park("i1")
        return await agent_resume("i1", "the answer")

    assert asyncio.run(go()) == "the caller handled it"
    failure = {"status": "error", "error_type": "RuntimeError", "error": "provider down"}
    assert stages.settled == [(CALLER_KEY, RunFailed(outcome=failure))]
    assert chain_tool.fired == [{"chain_token": CALLER_KEY, "result": failure, "status": PARK_COMPLETION_FAILED}]


def test_a_take_over_returns_the_terminal_the_stages_of_the_captured_routing_settle(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool, resumer_tool: _ChainTool, stages: _Stages
) -> None:
    stages.mapped[CALLER_KEY] = "mapped"
    agent.body = _returning("the final answer")

    async def go() -> Any:
        await _park("i1")
        cont = set_resume_continuation_tool("holder_resume")
        chain = set_chained_resume(
            ChainedResume(delivery_tool="probe_resumer_deliver", chain_key=RESUMER_KEY, asked_by=("resumer",))
        )
        try:
            return await agent_resume("i1", "x")
        finally:
            reset_chained_resume(chain)
            reset_resume_continuation_tool(cont)

    assert asyncio.run(go()) == {"mapped": "the final answer"}
    assert stages.settled == [(CALLER_KEY, "the final answer")]
    assert resumer_tool.fired == []
    assert [(f["chain_token"], f["status"]) for f in chain_tool.fired] == [(CALLER_KEY, PARK_COMPLETION_FAILED)]


def test_two_chained_levels_each_settle_their_terminal_under_their_own_key(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool, stages: _Stages, app_tools: Any
) -> None:
    # The agent parked on its call over a nested driver (the inner key) and is itself a call of the
    # caller (the caller key). The nested driver's terminal reaches the agent mapped by the inner
    # stage, and the agent's own terminal reaches the caller mapped by the outer stage.
    stages.mapped[INNER_KEY] = "inner"
    stages.mapped[CALLER_KEY] = "outer"
    agent.body = _returning("the agent's terminal")
    app_tools.tool_runners[CHAINED_PARK_DELIVERY_TOOL_NAME] = deliver_chained_park
    inner = ChainedResume(delivery_tool=CHAINED_PARK_DELIVERY_TOOL_NAME, chain_key=INNER_KEY, asked_by=("agent",))

    async def go() -> Any:
        await _park(INNER_KEY)
        return await fire_terminal_chain(inner, "the nested terminal")

    assert asyncio.run(go()) == "the caller handled it"
    assert agent.answers == [{"inner": "the nested terminal"}]
    assert stages.settled == [(INNER_KEY, "the nested terminal"), (CALLER_KEY, "the agent's terminal")]
    assert chain_tool.fired == [
        {"chain_token": CALLER_KEY, "result": {"outer": "the agent's terminal"}, "status": PARK_COMPLETION_SUCCEEDED}
    ]
