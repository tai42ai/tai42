"""The driver contract of the agents resume face: the outcome conversion, the two faces'
resume authorisation, the whole-chain kill teardown, and the horizon-derived resolution TTL.

The agents' park index is bound to an in-memory fakeredis; the bound recording app supplies the
platform facets the driver calls (``assert_resume_authorized`` / ``redelivery_horizon_seconds``
and ``run_tool`` for a cross-driver chain fire).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fakeredis import aioredis
from tai42_contract.interactions import (
    ParkResumeUnauthorizedError,
    ResumeBuffered,
    RunFailed,
    RunTerminalFailed,
    SuspendedInteraction,
)
from tai42_kit.interactions.park_index import (
    DriveInProgressError,
    LeaseLostError,
    ParkIndex,
    ResolutionMissingError,
    ResolutionRecord,
    superstep_id,
)
from tests.conftest import APP, bind_park_index

from tai42_agents._internal.park import park_binding
from tai42_agents._internal.park.chain import deliver_chained_park
from tai42_agents._internal.park.errors import ParkKillNotReadyError
from tai42_agents._internal.park.park_binding import agents_park_index
from tai42_agents._internal.park.resume import agent_resume, to_contract_outcome
from tai42_agents._internal.park.resume_tool import agent_park_kill_handler, agent_resume_tool

ANCESTOR = {"chain_key": "tai42:chained-park:anc", "asked_by": ["caller"]}


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    redis = aioredis.FakeRedis(decode_responses=True)
    bind_park_index(monkeypatch, redis)
    return redis


async def _write_park(
    interaction_ids: list[str],
    *,
    thread_id: str = "t",
    completion_tool: str | None = None,
    completion_context: dict[str, Any] | None = None,
    agent_name: str = "tools_agent",
) -> str:
    step = superstep_id(interaction_ids)
    entries = {
        iid: {
            "agent_name": agent_name,
            "thread_id": thread_id,
            "superstep_id": step,
            "interrupt_id": "int1",
            "rebuild_kwargs": {"checkpoint_provider": "redis", "recursion_limit": 50},
            "completion_tool": completion_tool,
            "completion_context": completion_context,
        }
        for iid in interaction_ids
    }
    await agents_park_index().persist(
        thread_id=thread_id,
        superstep=step,
        entries=entries,
        expected=dict.fromkeys(interaction_ids),
        entry_ttl=dict.fromkeys(interaction_ids, 100),
        barrier_ttl=100,
    )
    return step


async def _seed_finalized(thread_id: str, step: str, ids: list[str], *, resolution: Any, value: Any) -> None:
    """Claim the drive lease and finalize the super-step under it, as the drive and the kill both do."""
    index = agents_park_index()
    async with index.claim(thread_id, step) as lease:
        await index.finalize(lease, member_ids=ids, resolution=resolution, value=value)


def _is_resolved(entry: Any) -> bool:
    return entry is not None and agents_park_index().tombstone_kind(entry) == "resolved"


# ---- the outcome conversion ------------------------------------------------------------------


def test_conversion_a_run_failed_raises_on_the_platform_face_and_returns_on_the_chain_face() -> None:
    failed = RunFailed(outcome={"status": "error", "error": "down"})
    assert to_contract_outcome(failed) is failed
    with pytest.raises(RunTerminalFailed) as raised:
        to_contract_outcome(failed, raise_failed=True)
    assert raised.value.outcome == {"status": "error", "error": "down"}


def test_conversion_never_reads_a_status_word_of_a_plain_value() -> None:
    # Another driver's plain answer is returned whole, whatever words it carries.
    for value in ({"status": "error"}, {"status": "aborted"}, {"status": "buffered", "remaining_ids": ["x"]}):
        assert to_contract_outcome(value, raise_failed=True) == value


def test_conversion_suspended_repark_sentinel_passes_through_with_both_id_lists() -> None:
    sentinel = SuspendedInteraction(interaction_id="i1", interaction_ids=["i1", "i2"], caller_interaction_ids=["i2"])
    out = to_contract_outcome(sentinel)
    assert out is sentinel
    assert out.interaction_ids == ["i1", "i2"]
    assert out.caller_interaction_ids == ["i2"]
    assert out.resume_owner is None


def test_conversion_passes_contract_typed_and_plain_values_through() -> None:
    buffered = ResumeBuffered(remaining_ids=["i9"])
    assert to_contract_outcome(buffered) is buffered
    assert to_contract_outcome("the final answer") == "the final answer"
    assert to_contract_outcome(None) is None


# ---- resume authorisation at both faces -----------------------------------------------------


def test_agent_resume_tool_refuses_an_unauthorised_caller(fake_park_redis: Any, app_interactions: Any) -> None:
    async def go() -> None:
        step = await _write_park(["i1"])
        # A resolved super-step exists, so a leak past the assert would disclose an outcome; the
        # assert refuses BEFORE any park state is read.
        await _seed_finalized("t", step, ["i1"], resolution="terminal", value="secret")
        app_interactions.resume_authorized = False
        with pytest.raises(ParkResumeUnauthorizedError):
            await agent_resume_tool("i1", "x")
        assert app_interactions.resume_auth_calls == ["i1"]

    asyncio.run(go())


def test_deliver_chained_park_refuses_an_unauthorised_caller(fake_park_redis: Any, app_interactions: Any) -> None:
    async def go() -> None:
        app_interactions.resume_authorized = False
        with pytest.raises(ParkResumeUnauthorizedError):
            await deliver_chained_park(chain_token="tai42:chained-park:x", result="y")
        assert app_interactions.resume_auth_calls == ["tai42:chained-park:x"]

    asyncio.run(go())


def test_agent_resume_tool_authorised_returns_a_contract_typed_outcome(fake_park_redis: Any) -> None:
    async def go() -> None:
        await _write_park(["iA", "iB"])
        out = await agent_resume_tool("iA", "answer-a")
        assert isinstance(out, ResumeBuffered)
        assert out.remaining_ids == ["iB"]

    asyncio.run(go())


# ---- the whole-chain kill teardown -------------------------------------------------------


def test_kill_handler_is_a_noop_for_an_interaction_it_never_parked(fake_park_redis: Any) -> None:
    asyncio.run(agent_park_kill_handler("not-ours", "cancelled"))


def test_kill_handler_is_a_noop_when_the_park_index_is_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    # The handler is registered globally and fires for EVERY driver's kill. A deployment that loaded
    # the agents plugin but configured no durable park index owns no parks: the handler answers "not
    # ours" WITHOUT reading the index.
    bind_park_index(monkeypatch, None, configured=False)

    @contextlib.asynccontextmanager
    async def _forbidden() -> AsyncIterator[Any]:
        raise AssertionError("the kill handler read the park index though it is unconfigured")
        yield  # pragma: no cover — keeps this a generator; the raise fires on first entry

    settings = park_binding.agents_park_redis_settings()
    monkeypatch.setattr(
        park_binding, "_bound", (settings, ParkIndex(park_binding.AGENTS_PARK_NAMESPACE, settings, client=_forbidden))
    )
    asyncio.run(agent_park_kill_handler("not-ours", "cancelled"))


def test_kill_handler_drops_a_resolved_super_steps_stored_outcome(fake_park_redis: Any) -> None:
    async def go() -> None:
        index = agents_park_index()
        step = await _write_park(["i1", "i2"])
        await _seed_finalized("t", step, ["i1", "i2"], resolution="terminal", value="held outcome")
        assert await index.read_resolution("t", step) is not None
        # A person erase reaches the killed interaction: its whole super-step's stored outcome
        # (which can hold person data) and its tombstones are dropped.
        await agent_park_kill_handler("i1", "erased")
        assert await index.read_resolution("t", step) is None
        assert await index.read_entry("i1") is None
        assert await index.read_entry("i2") is None
        assert await index.run_resolutions("t") == {}

    asyncio.run(go())


def test_kill_handler_aborts_a_live_park_and_fires_the_chain_upward(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        step = await _write_park(["i1"], completion_tool="deliver_ancestor_chain", completion_context=ANCESTOR)
        fired: list[dict[str, Any]] = []
        app_tools.tool_runners["deliver_ancestor_chain"] = lambda **kwargs: fired.append(kwargs)

        await agent_park_kill_handler("i1", "thread deleted")

        # The captured cross-driver chain was fired FAILED, carrying the ancestor's chain and the
        # aborted outcome, so an ancestor that waited on this run tears down too.
        assert fired == [
            {
                "chain_token": "tai42:chained-park:anc",
                "status": "failed",
                "result": {"status": "aborted", "reason": "thread deleted"},
            }
        ]
        (call,) = app_tools.run_tool_calls
        assert call["key"] == "deliver_ancestor_chain"
        assert call["continues_chain"] == ("caller",)
        # The super-step is finalized ``aborted`` with the aborted RunFailed.
        assert _is_resolved(await agents_park_index().read_entry("i1"))
        assert await agents_park_index().read_resolution("t", step) == ResolutionRecord(
            "aborted", RunFailed(outcome={"status": "aborted", "reason": "thread deleted"})
        )

    asyncio.run(go())


def test_kill_handler_chain_fire_propagates_so_the_kill_redelivers(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        step = await _write_park(["i1"], completion_tool="deliver_ancestor_chain", completion_context=ANCESTOR)

        def deliver_ancestor_chain(**_kwargs: Any) -> None:
            raise RuntimeError("ancestor unreachable")

        app_tools.tool_runners["deliver_ancestor_chain"] = deliver_ancestor_chain

        # The cross-driver teardown fire is NOT best-effort: its failure PROPAGATES so kill_park
        # keeps the kill-due record and the reaper redelivers; the lease is released for it.
        with pytest.raises(RuntimeError, match="ancestor unreachable"):
            await agent_park_kill_handler("i1", "cancelled")
        assert not _is_resolved(await agents_park_index().read_entry("i1"))
        assert await fake_park_redis.get(agents_park_index().lease_key("t", step)) is None

    asyncio.run(go())


def test_kill_handler_drops_the_whole_runs_resolution_records(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        index = agents_park_index()
        ss1 = await _write_park(["p1"])
        await _seed_finalized("t", ss1, ["p1"], resolution="terminal", value="out-1")
        ss2 = await _write_park(["p2"])
        await _seed_finalized("t", ss2, ["p2"], resolution="terminal", value="out-2")
        ss_live = await _write_park(["live"])

        await agent_park_kill_handler("live", "person erased")

        for ss, pid in ((ss1, "p1"), (ss2, "p2")):
            assert await index.read_resolution("t", ss) is None
            assert await index.read_entry(pid) is None
        assert _is_resolved(await index.read_entry("live"))
        assert await fake_park_redis.get(index.lease_key("t", ss_live)) is None
        live_record = await index.read_resolution("t", ss_live)
        assert live_record is not None
        assert live_record.resolution == "aborted"
        assert set(await index.run_resolutions("t")) == {ss_live}

        # A second kill of the same id drops the aborted super-step and no-ops.
        await agent_park_kill_handler("live", "person erased")
        assert await index.run_resolutions("t") == {}

    asyncio.run(go())


def test_kill_handler_aborted_tombstone_replays_as_run_terminal_failed(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        await _write_park(["i1"])
        await agent_park_kill_handler("i1", "cancelled")
        # A redrive of the killed super-step's due record replays the aborted RunFailed, which the
        # platform face raises so the platform delivers FAILED (once, deduped against the kill).
        with pytest.raises(RunTerminalFailed) as exc:
            await agent_resume_tool("i1", "late")
        assert exc.value.outcome == {"status": "aborted", "reason": "cancelled"}

    asyncio.run(go())


def test_kill_handler_defers_while_a_live_drive_holds_the_lease(fake_park_redis: Any, app_tools: Any) -> None:
    async def go() -> None:
        index = agents_park_index()
        step = await _write_park(["i1"], completion_tool="deliver_ancestor_chain", completion_context=ANCESTOR)
        app_tools.tool_runners["deliver_ancestor_chain"] = lambda **_kwargs: None
        await fake_park_redis.set(index.lease_key("t", step), "live-drive")

        with pytest.raises(ParkKillNotReadyError):
            await agent_park_kill_handler("i1", "thread deleted")

        # It wrote NOTHING: no chain fire, the live drive's lease is untouched, the park entry stays
        # live, the barrier stands, and no resolution record exists.
        assert app_tools.run_tool_calls == []
        assert await fake_park_redis.get(index.lease_key("t", step)) == "live-drive"
        assert not _is_resolved(await index.read_entry("i1"))
        assert await index.read_barrier("t", step) is not None
        assert await index.read_resolution("t", step) is None

        # Once the drive released its lease, the redelivered kill lands.
        await fake_park_redis.delete(index.lease_key("t", step))
        await agent_park_kill_handler("i1", "thread deleted")
        assert _is_resolved(await index.read_entry("i1"))
        assert await fake_park_redis.get(index.lease_key("t", step)) is None

    asyncio.run(go())


# ---- the horizon-derived resolution TTL --------------------------------------------------


def test_resolution_ttl_is_twice_the_platform_redelivery_horizon(fake_park_redis: Any, app_interactions: Any) -> None:
    async def go() -> None:
        app_interactions.redelivery_horizon = 1000
        index = agents_park_index()
        step = await _write_park(["i1"])
        await _seed_finalized("t", step, ["i1"], resolution="terminal", value="v")
        assert await fake_park_redis.ttl(index.resolution_key("t", step)) == 2000
        assert await fake_park_redis.ttl(index.entry_key("i1")) == 2000
        assert await fake_park_redis.ttl(index.run_resolutions_key("t")) == 2000

    asyncio.run(go())


# ---- the drive re-checks its lease before the terminal chain fire --------------------------


def test_drive_whose_lease_a_kill_took_does_not_fire_its_chain_and_raises(fake_park_redis: Any, app_tools: Any) -> None:
    class _Agent:
        async def aresume_park(self, *, rebuild_kwargs: Any, thread_id: str, resume_map: Any) -> Any:
            # The drive produced a clean terminal, but while it ran its lease was taken.
            await fake_park_redis.set(agents_park_index().lease_key(thread_id, superstep_id(["i1"])), "kill-token")
            return "leaf terminal"

    APP.agents.registry["lease_losing_agent"] = _Agent()  # type: ignore[assignment]
    app_tools.tool_runners["deliver_ancestor_chain"] = lambda **_kwargs: "ancestor result"

    async def go() -> None:
        step = await _write_park(
            ["i1"],
            completion_tool="deliver_ancestor_chain",
            completion_context=ANCESTOR,
            agent_name="lease_losing_agent",
        )
        # The drive observes the lost lease at the re-check before its chain fire: it raises and
        # fires NO chain routing (no SUCCEEDED cascade up to the waiting ancestor).
        with pytest.raises(LeaseLostError):
            await agent_resume("i1", "the answer")
        assert app_tools.run_tool_calls == []
        assert await agents_park_index().read_resolution("t", step) is None

    try:
        asyncio.run(go())
    finally:
        APP.agents.registry.pop("lease_losing_agent", None)


def test_drive_whose_lease_a_kill_reclaimed_leaves_the_kills_resolution_for_the_redrive(
    fake_park_redis: Any, app_tools: Any
) -> None:
    killed = RunFailed(outcome={"status": "aborted", "reason": "thread deleted"})

    class _Agent:
        async def aresume_park(self, *, rebuild_kwargs: Any, thread_id: str, resume_map: Any) -> Any:
            # The drive produced a clean terminal, but while it ran its lease lapsed and a
            # whole-chain kill reclaimed the super-step and finalized it aborted.
            index = agents_park_index()
            step = superstep_id(["i1"])
            await fake_park_redis.delete(index.lease_key(thread_id, step))
            async with index.claim(thread_id, step) as kill_lease:
                await index.finalize(kill_lease, member_ids=["i1"], resolution="aborted", value=killed)
            return "leaf terminal"

    APP.agents.registry["lease_losing_agent"] = _Agent()  # type: ignore[assignment]
    app_tools.tool_runners["deliver_ancestor_chain"] = lambda **_kwargs: "ancestor result"

    async def go() -> None:
        step = await _write_park(
            ["i1"],
            completion_tool="deliver_ancestor_chain",
            completion_context=ANCESTOR,
            agent_name="lease_losing_agent",
        )
        with pytest.raises(LeaseLostError):
            await agent_resume("i1", "the answer")
        assert app_tools.run_tool_calls == []

        # The kill's aborted resolution stands, unmodified by the drive; a redrive replays it and
        # the platform face raises it FAILED.
        assert await agents_park_index().read_resolution("t", step) == ResolutionRecord("aborted", killed)
        assert await agent_resume("i1", "the answer") == killed
        with pytest.raises(RunTerminalFailed) as exc:
            await agent_resume_tool("i1", "the answer")
        assert exc.value.outcome == {"status": "aborted", "reason": "thread deleted"}

    try:
        asyncio.run(go())
    finally:
        APP.agents.registry.pop("lease_losing_agent", None)


# ---- the FAILED-terminal raise and the resolution-record encoding -------------------------


@pytest.mark.parametrize("status", ["error", "stopped", "aborted"])
def test_conversion_raises_a_failed_terminal_only_when_asked(status: str) -> None:
    failed = RunFailed(outcome={"status": status, "reason": "the run did not finish"})
    # The chain-fire face returns the failed outcome into the firing run's drive ...
    assert to_contract_outcome(failed) is failed
    # ... while the platform-continuation face raises it whole for the delivery ladder.
    with pytest.raises(RunTerminalFailed) as exc:
        to_contract_outcome(failed, raise_failed=True)
    assert exc.value.outcome == failed.outcome


@pytest.mark.parametrize(
    "outcome",
    [
        SuspendedInteraction(interaction_id="i1", interaction_ids=["i1", "i2"], caller_interaction_ids=["i2"]),
        ResumeBuffered(remaining_ids=["i2"]),
        RunFailed(outcome={"status": "error", "error": "down"}),
        {"decision": "approved"},
        "the final answer",
    ],
    ids=["suspended", "buffered", "failed", "raw-dict", "raw-str"],
)
def test_resolution_record_round_trips_each_outcome_type(fake_park_redis: Any, outcome: Any) -> None:
    async def go() -> None:
        step = await _write_park(["i1"])
        await _seed_finalized("t", step, ["i1"], resolution="terminal", value=outcome)
        record = await agents_park_index().read_resolution("t", step)
        assert record is not None
        assert type(record.value) is type(outcome)
        assert record.value == outcome

    asyncio.run(go())


# ---- the drive: replay gaps, rejections, and the outcome a drive resolves to --------------------


class _ScriptedResumeAgent:
    """An agent whose ``aresume_park`` face returns, or raises, what a test scripts."""

    def __init__(self, outcome: Any = None, *, raises: BaseException | None = None) -> None:
        self.outcome = outcome
        self.raises = raises
        self.resume_maps: list[dict[str, dict[str, Any]]] = []

    async def aresume_park(self, *, rebuild_kwargs: dict[str, Any], thread_id: str, resume_map: Any) -> Any:
        self.resume_maps.append(resume_map)
        if self.raises is not None:
            raise self.raises
        return self.outcome


def _bind_agent(monkeypatch: pytest.MonkeyPatch, agent: Any) -> None:
    # Every park ``_write_park`` seeds names ``tools_agent``; the scripted face answers for it.
    monkeypatch.setitem(APP.agents.registry, "tools_agent", agent)


async def _lease_is_free(thread_id: str, step: str) -> bool:
    """Whether another worker can win the super-step's drive lease now (won, then released)."""
    try:
        lease = await agents_park_index().claim(thread_id, step).acquire()
    except DriveInProgressError:
        return False
    await lease.close()
    return True


def test_agent_resume_on_a_tombstone_whose_record_is_gone_raises(fake_park_redis: Any) -> None:
    async def go() -> None:
        index = agents_park_index()
        step = await _write_park(["i1"])
        await _seed_finalized("t", step, ["i1"], resolution="terminal", value="done")
        # The resolution record is gone while its resolved tombstone stands: nothing is left to
        # replay, which is never read as a silent no-op.
        await fake_park_redis.delete(index.resolution_key("t", step))
        assert _is_resolved(await index.read_entry("i1"))
        with pytest.raises(ResolutionMissingError):
            await agent_resume("i1", "late answer")

    asyncio.run(go())


def test_agent_resume_rejects_an_answer_the_super_step_does_not_expect(fake_park_redis: Any) -> None:
    from tai42_agents._internal.park.errors import AgentResumeInterruptNotPendingError

    async def go() -> None:
        index = agents_park_index()
        step = superstep_id(["i1"])
        entry = {
            "agent_name": "tools_agent",
            "thread_id": "t",
            "superstep_id": step,
            "interrupt_id": "int1",
            "rebuild_kwargs": {},
            "completion_tool": None,
            "completion_context": None,
        }
        # ``stray`` holds a park entry pointing at the super-step, but the barrier expects only i1.
        await index.persist(
            thread_id="t",
            superstep=step,
            entries={"i1": entry, "stray": entry},
            expected={"i1": None},
            entry_ttl={"i1": 100, "stray": 100},
            barrier_ttl=100,
        )
        with pytest.raises(AgentResumeInterruptNotPendingError):
            await agent_resume("stray", "answer")
        barrier = await index.read_barrier("t", step)
        assert barrier is not None
        assert barrier.outputs == {}

    asyncio.run(go())


def test_drive_that_parks_again_resolves_the_super_step_suspended(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    repark = SuspendedInteraction(interaction_id="i-next", interaction_ids=["i-next"], caller_interaction_ids=[])
    agent = _ScriptedResumeAgent(repark)
    _bind_agent(monkeypatch, agent)

    async def go() -> None:
        step = await _write_park(["i1"])
        assert await agent_resume("i1", "yes") is repark
        assert agent.resume_maps == [{"int1": {"i1": "yes"}}]
        record = await agents_park_index().read_resolution("t", step)
        assert record is not None
        assert record.resolution == "suspended"
        # A redrive replays the re-park as the same contract type.
        replayed = await agent_resume("i1", "yes")
        assert type(replayed) is SuspendedInteraction
        assert replayed == repark

    asyncio.run(go())


def test_drive_terminal_of_a_nested_run_returns_the_ancestors_chain_fire(
    fake_park_redis: Any, app_tools: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind_agent(monkeypatch, _ScriptedResumeAgent("leaf terminal"))

    async def go() -> None:
        step = await _write_park(["i1"], completion_tool="deliver_ancestor_chain", completion_context=ANCESTOR)
        app_tools.tool_runners["deliver_ancestor_chain"] = lambda **_kwargs: "outermost result"
        # The leaf's terminal fires the ancestor's chain-delivery tool; its return is the outcome.
        assert await agent_resume("i1", "answer") == "outermost result"
        assert len(app_tools.run_tool_calls) == 1
        assert await agents_park_index().read_resolution("t", step) == ResolutionRecord("terminal", "outermost result")

    asyncio.run(go())


def test_drive_superseded_mid_resume_raises_failed_and_finalizes_the_super_step_aborted(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tai42_contract.conversations import TurnSupersededError

    _bind_agent(monkeypatch, _ScriptedResumeAgent(raises=TurnSupersededError("m2")))

    async def go() -> None:
        index = agents_park_index()
        step = await _write_park(["i1"])
        with pytest.raises(RunTerminalFailed) as exc:
            await agent_resume("i1", "answer")
        aborted = {"status": "aborted", "reason": "TurnSupersededError"}
        assert exc.value.outcome == aborted
        # The supersede is the run's failed terminal: the super-step is finalized ``aborted`` with
        # it under the drive's lease, which the finalize released.
        assert await index.read_resolution("t", step) == ResolutionRecord("aborted", RunFailed(outcome=aborted))
        assert _is_resolved(await index.read_entry("i1"))
        assert await fake_park_redis.get(index.lease_key("t", step)) is None

    asyncio.run(go())


def test_drive_of_an_agent_without_a_resume_face_raises_and_releases_the_lease(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _bind_agent(monkeypatch, object())

    async def go() -> None:
        step = await _write_park(["i1"])
        with pytest.raises(RuntimeError, match="exposes no aresume_park face"):
            await agent_resume("i1", "answer")
        assert await _lease_is_free("t", step)
        assert await agents_park_index().read_resolution("t", step) is None
        assert not _is_resolved(await agents_park_index().read_entry("i1"))

    asyncio.run(go())


def test_drive_raises_when_a_siblings_park_entry_is_gone(fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from tai42_agents._internal.park.errors import AgentResumeParkEntryNotFoundError

    agent = _ScriptedResumeAgent("never reached")
    _bind_agent(monkeypatch, agent)

    async def go() -> None:
        index = agents_park_index()
        step = await _write_park(["i1", "i2"])
        assert await agent_resume("i2", "b") == ResumeBuffered(remaining_ids=["i1"])
        # i2's park entry is gone (aged out) before the last answer completes the barrier.
        await fake_park_redis.delete(index.entry_key("i2"))
        with pytest.raises(AgentResumeParkEntryNotFoundError):
            await agent_resume("i1", "a")
        assert agent.resume_maps == []
        assert await _lease_is_free("t", step)

    asyncio.run(go())
