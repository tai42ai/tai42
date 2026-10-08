"""The agents' resume drive ends a resumed run that raises as a FAILED terminal, chained or not.

A resumed run whose graph raises (a model/provider error past the client's retries, a
``RunTerminalFailed`` inside it, a supersede or a cancellation) reaches its failed terminal: a
chained park fires its captured routing ``failed`` and finalizes with what the fire returned; an
unchained park finalizes its ``RunFailed`` and raises ``RunTerminalFailed`` for the platform to
deliver FAILED. A raise of the PREPARE step (the run was never resumed) stays a plain raise.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

import pytest
from tai42_contract.conversations import TurnSupersededError
from tai42_contract.interactions import (
    PARK_COMPLETION_FAILED,
    ResumeBuffered,
    RunFailed,
    RunTerminalFailed,
    SuspendedInteraction,
)
from tai42_kit.interactions.park_index import (
    LeaseLostError,
    ResolutionRecord,
    entry_ttl_seconds,
    superstep_id,
)
from tests.conftest import APP

from tai42_agents._internal.park.chain import deliver_chained_park
from tai42_agents._internal.park.drive import park_drive
from tai42_agents._internal.park.errors import WorkspaceLeaseHeldError
from tai42_agents._internal.park.park_binding import agents_park_index
from tai42_agents._internal.park.resume import agent_resume
from tai42_agents._internal.park.resume_tool import agent_resume_tool

THREAD = "t-failed"
AGENT = "probe_resumable_agent"
CALLER_KEY = "tai42:chained-park:caller"
PROVIDER_DOWN = {"status": "error", "error_type": "RuntimeError", "error": "provider down"}


class _ResumableAgent:
    """A registered agent whose ``aresume_park`` runs a scripted body."""

    def __init__(self) -> None:
        self.body: Callable[[], Any] = lambda: "resumed"
        self.calls = 0

    async def aresume_park(self, *, rebuild_kwargs: Any, thread_id: str, resume_map: Any) -> Any:
        self.calls += 1
        return await self.body()


@pytest.fixture
def agent() -> Any:
    probe = _ResumableAgent()
    APP.agents.registry[AGENT] = probe  # type: ignore[assignment]
    yield probe
    APP.agents.registry.pop(AGENT, None)


class _ChainTool:
    """A test chain-delivery tool: records each fire's payload and returns ``reply``."""

    def __init__(self) -> None:
        self.fired: list[dict[str, Any]] = []
        self.reply: Any = "the caller handled it"

    def __call__(self, **payload: Any) -> Any:
        self.fired.append(payload)
        return self.reply


@pytest.fixture
def chain_tool(app_tools: Any) -> _ChainTool:
    tool = _ChainTool()
    app_tools.tool_runners["probe_chain_deliver"] = tool
    return tool


async def _park(ids: list[str], *, chained: bool) -> str:
    step = superstep_id(ids)
    entries = {
        iid: {
            "agent_name": AGENT,
            "thread_id": THREAD,
            "superstep_id": step,
            "interrupt_id": "int-1",
            "rebuild_kwargs": {},
            "completion_tool": "probe_chain_deliver" if chained else None,
            "completion_context": {"chain_key": CALLER_KEY, "asked_by": ["caller"]} if chained else None,
        }
        for iid in ids
    }
    await agents_park_index().persist(
        thread_id=THREAD,
        superstep=step,
        entries=entries,
        expected=dict.fromkeys(ids),
        entry_ttl={iid: entry_ttl_seconds(None) for iid in ids},
        barrier_ttl=entry_ttl_seconds(None),
    )
    return step


def _raising(exc: BaseException) -> Callable[[], Any]:
    async def body() -> Any:
        raise exc

    return body


async def _record(step: str) -> ResolutionRecord | None:
    return await agents_park_index().read_resolution(THREAD, step)


# ---- the model/provider-error arm --------------------------------------------------------------


def test_a_chained_provider_error_fires_failed_and_returns_the_callers_answer(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool, app_tools: Any
) -> None:
    async def go() -> None:
        step = await _park(["i1"], chained=True)
        agent.body = _raising(RuntimeError("provider down"))

        outcome = await agent_resume("i1", "the answer")

        assert outcome == "the caller handled it"
        assert chain_tool.fired == [
            {"chain_token": CALLER_KEY, "result": PROVIDER_DOWN, "status": PARK_COMPLETION_FAILED}
        ]
        assert app_tools.run_tool_calls[0]["continues_chain"] == ("caller",)
        assert await _record(step) == ResolutionRecord("terminal", "the caller handled it")

    asyncio.run(go())


def test_a_chained_fire_that_returns_run_failed_is_raised_by_the_platform_face(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool
) -> None:
    async def go() -> None:
        step = await _park(["i1"], chained=True)
        agent.body = _raising(RuntimeError("provider down"))
        chain_tool.reply = RunFailed(outcome={"caller": "failed too"})

        with pytest.raises(RunTerminalFailed) as raised:
            await agent_resume_tool("i1", "the answer")

        assert raised.value.outcome == {"caller": "failed too"}
        assert await _record(step) == ResolutionRecord("terminal", RunFailed(outcome={"caller": "failed too"}))

    asyncio.run(go())


def test_an_unchained_provider_error_finalizes_failed_and_raises(
    park_redis: Any, agent: _ResumableAgent, app_tools: Any, caplog: pytest.LogCaptureFixture
) -> None:
    async def go() -> None:
        step = await _park(["i1"], chained=False)
        agent.body = _raising(RuntimeError("provider down"))

        with pytest.raises(RunTerminalFailed) as raised:
            await agent_resume("i1", "the answer")

        assert raised.value.outcome == PROVIDER_DOWN
        assert app_tools.run_tool_calls == []
        assert await _record(step) == ResolutionRecord("terminal", RunFailed(outcome=PROVIDER_DOWN))
        # A lapped redelivery replays the stored failure on the platform face.
        with pytest.raises(RunTerminalFailed) as replayed:
            await agent_resume_tool("i1", "late")
        assert replayed.value.outcome == PROVIDER_DOWN
        assert agent.calls == 1

    asyncio.run(go())
    logged = [r for r in caplog.records if r.levelno == logging.ERROR and "ending it FAILED" in r.getMessage()]
    assert len(logged) == 1
    assert logged[0].exc_info is not None
    assert str(logged[0].exc_info[1]) == "provider down"


# ---- the RunTerminalFailed arm ------------------------------------------------------------------


def test_a_run_terminal_failed_inside_the_resumed_run_carries_its_outcome(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool
) -> None:
    async def go() -> None:
        await _park(["i1"], chained=True)
        agent.body = _raising(RunTerminalFailed({"k": "v"}))
        await agent_resume("i1", "x")
        assert chain_tool.fired[0]["result"] == {"k": "v"}
        assert chain_tool.fired[0]["status"] == PARK_COMPLETION_FAILED

        await _park(["i2"], chained=False)
        with pytest.raises(RunTerminalFailed) as raised:
            await agent_resume("i2", "x")
        assert raised.value.outcome == {"k": "v"}

    asyncio.run(go())


# ---- the supersede / cancel arm ---------------------------------------------------------------


@pytest.mark.parametrize("exc_type", [TurnSupersededError, asyncio.CancelledError])
def test_a_chained_abort_fires_the_aborted_outcome(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool, exc_type: type[BaseException]
) -> None:
    async def go() -> None:
        step = await _park(["i1"], chained=True)
        agent.body = _raising(exc_type("successor-turn") if exc_type is TurnSupersededError else exc_type())
        if exc_type is asyncio.CancelledError:
            with pytest.raises(asyncio.CancelledError):
                await agent_resume("i1", "x")
        else:
            assert await agent_resume("i1", "x") == "the caller handled it"
        assert chain_tool.fired[0]["result"] == {"status": "aborted", "reason": exc_type.__name__}
        assert await _record(step) == ResolutionRecord("terminal", "the caller handled it")

    asyncio.run(go())


def test_an_unchained_supersede_finalizes_aborted_and_raises(park_redis: Any, agent: _ResumableAgent) -> None:
    async def go() -> None:
        step = await _park(["i1"], chained=False)
        agent.body = _raising(TurnSupersededError("successor-turn"))
        with pytest.raises(RunTerminalFailed) as raised:
            await agent_resume("i1", "x")
        aborted = {"status": "aborted", "reason": "TurnSupersededError"}
        assert raised.value.outcome == aborted
        assert await _record(step) == ResolutionRecord("aborted", RunFailed(outcome=aborted))

    asyncio.run(go())


def test_an_unchained_cancel_is_re_raised_after_the_finalize(park_redis: Any, agent: _ResumableAgent) -> None:
    async def go() -> None:
        step = await _park(["i1"], chained=False)
        agent.body = _raising(asyncio.CancelledError())
        with pytest.raises(asyncio.CancelledError):
            await agent_resume("i1", "x")
        aborted = {"status": "aborted", "reason": "CancelledError"}
        assert await _record(step) == ResolutionRecord("aborted", RunFailed(outcome=aborted))

    asyncio.run(go())


# ---- the lease-lost arm and the reclaimed lease ----------------------------------------------------


@pytest.mark.parametrize("busy", ["lease_lost", "workspace_held"])
def test_a_lease_raise_from_the_resumed_run_is_re_raised_with_no_fire(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool, busy: str
) -> None:
    # A kill owning the super-step, or another drive holding the run's workspace, ends no run:
    # the raise propagates, nothing is fired or finalized, and the index stays live.
    async def go() -> None:
        step = await _park(["i1"], chained=True)
        raised: BaseException = (
            LeaseLostError(THREAD, step) if busy == "lease_lost" else WorkspaceLeaseHeldError("ws-key")
        )
        agent.body = _raising(raised)
        with pytest.raises(type(raised)):
            await agent_resume("i1", "x")
        assert chain_tool.fired == []
        assert await _record(step) is None
        entry = await agents_park_index().read_entry("i1")
        assert entry is not None
        assert agents_park_index().tombstone_kind(entry) == "live"

    asyncio.run(go())


def test_a_lease_reclaimed_by_a_kill_during_the_drive_fires_nothing(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool
) -> None:
    async def go() -> None:
        step = await _park(["i1"], chained=True)
        index = agents_park_index()

        async def stolen() -> Any:
            await park_redis.set(index.lease_key(THREAD, step), "kill-token")
            raise RuntimeError("provider down")

        agent.body = stolen
        with pytest.raises(LeaseLostError):
            await agent_resume("i1", "x")
        assert chain_tool.fired == []
        assert await _record(step) is None
        assert await park_redis.get(index.lease_key(THREAD, step)) == "kill-token"

    asyncio.run(go())


# ---- the PREPARE arm ------------------------------------------------------------------------


def test_a_prepare_step_raise_is_plain_with_no_fire_no_finalize_and_the_lease_released(
    park_redis: Any, chain_tool: _ChainTool
) -> None:
    async def go() -> None:
        step = await _park(["i1"], chained=True)
        index = agents_park_index()
        # The agent the entry names is not registered: the prepare step raises before any resume.
        with pytest.raises(RuntimeError, match="No such agent"):
            await agent_resume("i1", "x")
        assert chain_tool.fired == []
        assert await _record(step) is None
        assert await park_redis.get(index.lease_key(THREAD, step)) is None

    asyncio.run(go())


def test_a_missing_sibling_entry_in_prepare_is_plain(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool
) -> None:
    async def go() -> None:
        step = await _park(["i1", "i2"], chained=True)
        index = agents_park_index()
        await agent_resume("i1", "a")
        await park_redis.delete(index.entry_key("i1"))
        with pytest.raises(Exception, match="i1"):
            await agent_resume("i2", "b")
        assert chain_tool.fired == []
        assert agent.calls == 0
        assert await park_redis.get(index.lease_key(THREAD, step)) is None

    asyncio.run(go())


def test_the_failure_finalize_tombstones_exactly_the_barriers_members(park_redis: Any, agent: _ResumableAgent) -> None:
    async def go() -> None:
        step = await _park(["i1", "i2"], chained=False)
        agent.body = _raising(RuntimeError("provider down"))
        assert isinstance(await agent_resume("i1", "a"), ResumeBuffered)
        with pytest.raises(RunTerminalFailed):
            await agent_resume("i2", "b")
        assert await agents_park_index().run_resolutions(THREAD) == {step: ["i1", "i2"]}

    asyncio.run(go())


# ---- the chain-delivery face ----------------------------------------------------------------


def test_an_unknown_chain_status_raises(park_redis: Any) -> None:
    async def go() -> None:
        with pytest.raises(ValueError, match="unknown chain status 'weird'"):
            await deliver_chained_park(chain_token=CALLER_KEY, result=1, status="weird")

    asyncio.run(go())


def test_a_run_failed_returned_up_a_chain_is_returned_by_the_chain_face(
    park_redis: Any, agent: _ResumableAgent, chain_tool: _ChainTool
) -> None:
    async def go() -> None:
        await _park([CALLER_KEY + "-nested"], chained=True)
        agent.body = _raising(RuntimeError("provider down"))
        chain_tool.reply = RunFailed(outcome={"ancestor": "failed"})
        returned = await deliver_chained_park(chain_token=CALLER_KEY + "-nested", result={"x": 1}, status="succeeded")
        assert returned == RunFailed(outcome={"ancestor": "failed"})

    asyncio.run(go())


def test_a_re_park_value_round_trips_the_resolution_record(park_redis: Any, agent: _ResumableAgent) -> None:
    async def go() -> None:
        step = await _park(["i1"], chained=False)
        re_park = SuspendedInteraction(interaction_id="i9")

        async def parks_again() -> Any:
            return re_park

        agent.body = parks_again
        assert await agent_resume("i1", "x") == re_park
        assert await _record(step) == ResolutionRecord("suspended", re_park)

    asyncio.run(go())


# ---- the detach after a drive (behaviour kept) --------------------------------------------------


def test_a_failed_detach_is_logged_and_the_drive_result_is_kept(
    park_redis: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from tai42_contract.interactions.continuation import _chained_park_claims

    index = agents_park_index()

    async def broken_detach(_ids: Any) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(index, "detach", broken_detach)

    async def go() -> str:
        async with park_drive(None):
            claims = _chained_park_claims.get()
            assert claims is not None
            claims.add("tai42:chained-park:dead")
            return "the drive result"

    assert asyncio.run(go()) == "the drive result"
    assert any(r.levelno == logging.WARNING and "could not detach" in r.getMessage() for r in caplog.records)
