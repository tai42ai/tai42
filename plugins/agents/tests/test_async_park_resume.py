"""The agent async-park resume driver + durable index.

Park-capability gating, the ``agent_resume`` continuation (buffered/lost-lease/not-found),
and the full cross-worker cycle — a real ``DeepAgent.run`` parks and returns a suspended
receipt, then ``agent_resume`` rebuilds the graph on a fresh runtime (same checkpointer +
the durable park index) and drives it to completion, running the parked tool exactly once.

The park index is backed by an in-memory fakeredis routed in through the index module's
``client_ctx`` seam; the checkpoint is an ``InMemorySaver`` shared between the park run and
the resume, standing in for the durable checkpoint a cross-worker resume reads.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore
from pydantic import PrivateAttr
from tai42_contract.app import tai42_app
from tai42_contract.interactions import get_resume_continuation_tool, suspended_interaction_marker

from tai42_agents._internal.park import agent_resume, build_park_identity
from tai42_agents._internal.park import driver as drv
from tai42_agents._internal.park import index as idx
from tai42_agents._internal.park.errors import (
    AgentParkNotHostableError,
    AgentResumeDriveInProgressError,
    AgentResumeInterruptNotPendingError,
    AgentResumeParkEntryNotFoundError,
    ParkExpiryExceedsRetentionError,
)
from tai42_agents.langchain_deep_agent import agent as agent_mod
from tai42_agents.langchain_deep_agent.tool_spec import DeepSubAgentSpec


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


# A park deadline comfortably WITHIN the durable-workspace retention horizon (session_ttl,
# 24h by default): the deep agent's run now acquires a persistent workspace whose idle-reap TTL
# bounds the park's retention to min(checkpoint, workspace) (§B3.1), so a full-run park must
# carry an ask deadline within it — a None-deadline ("wait forever") park is now correctly
# refused because the workspace would reap first.
_WITHIN_HORIZON = datetime.now(UTC) + timedelta(hours=1)
_WITHIN_HORIZON_ISO = _WITHIN_HORIZON.isoformat()


class _CountingAsk:
    def __init__(self, interaction_id: str, expiry_at: datetime | None = _WITHIN_HORIZON) -> None:
        self.calls = 0
        self._interaction_id = interaction_id
        self._expiry_at = expiry_at

    def tool(self) -> StructuredTool:
        def ask() -> dict[str, Any]:
            self.calls += 1
            return suspended_interaction_marker(self._interaction_id, self._expiry_at, get_resume_continuation_tool())

        return StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")


def _ask_call(call_id: str = "c1") -> AIMessage:
    return AIMessage(content="", tool_calls=[{"id": call_id, "name": "ask", "args": {}}])


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


# ---- park capability ------------------------------------------------------


def test_build_park_identity_captures_a_durable_rebuildable_run(fake_park_redis: Any) -> None:
    config = {"configurable": {"thread_id": "t1"}}
    park = build_park_identity(
        agent_name="langchain_deep_agent",
        config=config,
        checkpoint_provider="redis",
        has_live_tools=False,
        rebuild_kwargs={"tool_names": ["ask"]},
        recursion_limit=50,
        bind=True,
    )
    assert park is not None
    # The provider-free identity carries no checkpoint provider itself: the resolved durable
    # provider and the recursion limit are pinned INTO the rebuild identity instead.
    assert park.rebuild_kwargs["checkpoint_provider"] == "redis"
    assert park.rebuild_kwargs["recursion_limit"] == 50
    assert not hasattr(park, "checkpoint_provider")
    assert park.thread_id == "t1"
    assert park.bind is True


def test_build_park_identity_refuses_non_durable_checkpoint(fake_park_redis: Any) -> None:
    park = build_park_identity(
        agent_name="langchain_deep_agent",
        config={"configurable": {"thread_id": "t1"}},
        checkpoint_provider="memory",
        has_live_tools=False,
        rebuild_kwargs={},
        recursion_limit=50,
        bind=True,
    )
    assert park is None


def test_build_park_identity_refuses_live_tools(fake_park_redis: Any) -> None:
    park = build_park_identity(
        agent_name="langchain_deep_agent",
        config={"configurable": {"thread_id": "t1"}},
        checkpoint_provider="redis",
        has_live_tools=True,
        rebuild_kwargs={},
        recursion_limit=50,
        bind=True,
    )
    assert park is None


def test_build_park_identity_refuses_non_serializable_rebuild(fake_park_redis: Any) -> None:
    park = build_park_identity(
        agent_name="langchain_deep_agent",
        config={"configurable": {"thread_id": "t1"}},
        checkpoint_provider="redis",
        has_live_tools=False,
        rebuild_kwargs={"x": object()},
        recursion_limit=50,
        bind=True,
    )
    assert park is None


def test_build_park_identity_refuses_unconfigured_park_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(drv, "agents_park_redis_settings", lambda: SimpleNamespace(redis_url=None))
    park = build_park_identity(
        agent_name="langchain_deep_agent",
        config={"configurable": {"thread_id": "t1"}},
        checkpoint_provider="redis",
        has_live_tools=False,
        rebuild_kwargs={},
        recursion_limit=50,
        bind=True,
    )
    assert park is None


# ---- expiry-vs-retention gate ---------------------------------------------


def _park_identity(retention_bound: datetime | None = None) -> drv.ParkIdentity:
    """A directly-constructed provider-free identity: the caller passes the retention bound
    (a datetime, or ``None`` for keep-forever) that the generalized persist gate reads."""
    return drv.ParkIdentity(
        agent_name="langchain_deep_agent",
        thread_id="t-gate",
        rebuild_kwargs={},
        bind=True,
        retention_bound=retention_bound,
    )


def _iso_in(minutes: float) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat()


def _bound_in(minutes: float) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


def test_park_persist_allows_expiry_within_retention(fake_park_redis: Any) -> None:
    async def go() -> None:
        interactions = {"i1": _iso_in(30), "i2": _iso_in(10)}
        await drv.persist_park(_park_identity(_bound_in(60)), [("int1", interactions)])
        assert await idx.read_park_entry("i1") is not None
        assert await idx.read_park_entry("i2") is not None

    asyncio.run(go())


def test_park_persist_refuses_expiry_beyond_retention_all_or_nothing(fake_park_redis: Any) -> None:
    async def go() -> None:
        # ``i2``'s deadline outlives the 60-minute retention bound.
        interactions = {"i1": _iso_in(30), "i2": _iso_in(120)}
        with pytest.raises(ParkExpiryExceedsRetentionError) as excinfo:
            await drv.persist_park(_park_identity(_bound_in(60)), [("int1", interactions)])
        assert excinfo.value.interaction_id == "i2"
        # All-or-nothing: not a single index key was written.
        assert await idx.read_park_entry("i1") is None
        assert await idx.read_park_entry("i2") is None

    asyncio.run(go())


def test_park_persist_refuses_mixed_within_and_beyond_all_or_nothing(fake_park_redis: Any) -> None:
    async def go() -> None:
        # Offender first, a valid sibling after it: the whole super-step still fails with no writes.
        interactions = {"i_bad": _iso_in(9999), "i_ok": _iso_in(5)}
        with pytest.raises(ParkExpiryExceedsRetentionError) as excinfo:
            await drv.persist_park(_park_identity(_bound_in(60)), [("int1", interactions)])
        assert excinfo.value.interaction_id == "i_bad"
        assert await idx.read_park_entry("i_bad") is None
        assert await idx.read_park_entry("i_ok") is None

    asyncio.run(go())


def test_park_persist_refuses_missing_expiry_under_bounded_retention(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A park with no deadline under a bounded retention is unresumable — refuse it loudly.
        interactions: dict[str, Any] = {"i1": _iso_in(10), "i2": None}
        with pytest.raises(ParkExpiryExceedsRetentionError) as excinfo:
            await drv.persist_park(_park_identity(_bound_in(60)), [("int1", interactions)])
        assert excinfo.value.interaction_id == "i2"
        assert excinfo.value.expiry_at is None
        assert await idx.read_park_entry("i1") is None
        assert await idx.read_park_entry("i2") is None

    asyncio.run(go())


def test_park_persist_allows_any_expiry_under_keep_forever_redis(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A ``None`` retention bound = keep-forever: a far-future deadline and a deadline-less
        # park both pass.
        interactions: dict[str, Any] = {"i1": _iso_in(10_000_000), "i2": None}
        await drv.persist_park(_park_identity(None), [("int1", interactions)])
        assert await idx.read_park_entry("i1") is not None
        assert await idx.read_park_entry("i2") is not None

    asyncio.run(go())


# ---- chained parks: the inherited horizon, and dead chains --------------------------------


_CHAIN = "tai42:chained-park:k1"


def test_a_chained_key_clamps_its_inherited_horizon_into_retention(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A chained key waits on a nested CALL: its deadline is INHERITED, not its own ask's, so
        # one beyond the retention bound is CLAMPED into it rather than failing the park —
        # nothing fires at a chained deadline, so a shortened one costs nothing, and the park
        # stays inside the window its own state survives.
        bound = _bound_in(60)
        written = await drv.persist_park(_park_identity(bound), [("int1", {_CHAIN: _iso_in(9999)})])
        assert datetime.fromisoformat(written[_CHAIN]) == bound
        assert await idx.read_park_entry(_CHAIN) is not None

    asyncio.run(go())


def test_a_chained_key_with_no_inherited_deadline_takes_the_cap(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def go() -> None:
        # Never unbounded: a nested run that carried no deadline at all still leaves the waiting
        # caller with a bounded park — an interaction id in the same position would be refused
        # under a bounded retention, because nothing could resume it.
        monkeypatch.setattr(drv, "agents_limits_settings", lambda: SimpleNamespace(chained_park_horizon_cap_hours=2))
        written = await drv.persist_park(_park_identity(None), [("int1", {_CHAIN: None})])
        capped = datetime.fromisoformat(written[_CHAIN])
        assert capped <= datetime.now(UTC) + timedelta(hours=2)
        entry = await idx.read_park_entry(_CHAIN)
        assert entry is not None
        # The bound the persist gated against rides the entry, so a later extension re-clamps
        # against the same one instead of guessing.
        assert entry["retention_bound"] is None

    asyncio.run(go())


def test_an_interaction_id_keeps_its_own_ask_deadline(fake_park_redis: Any) -> None:
    async def go() -> None:
        # Only chained keys are clamped: a real ask's deadline is its own and rides through
        # untouched, still meeting the retention gate on its own terms.
        deadline = _iso_in(30)
        written = await drv.persist_park(_park_identity(_bound_in(60)), [("int1", {"i1": deadline})])
        assert written == {"i1": deadline}

    asyncio.run(go())


def test_extending_a_park_horizon_never_shortens_it(fake_park_redis: Any) -> None:
    async def go() -> None:
        await drv.persist_park(_park_identity(None), [("int1", {_CHAIN: _iso_in(60)})])
        entry = await idx.read_park_entry(_CHAIN)
        assert entry is not None
        before = await fake_park_redis.ttl(f"agent:park:{_CHAIN}")
        # A NEARER deadline than the park already holds: an extension is not a re-sizing, so it
        # leaves both keys alone rather than cutting a park short.
        assert await idx.extend_park_horizon(_CHAIN, "t-gate", entry["superstep_id"], _bound_in(1)) is False
        assert await fake_park_redis.ttl(f"agent:park:{_CHAIN}") == before
        # A LATER one moves both the entry and the barrier out.
        assert (
            await idx.extend_park_horizon(
                _CHAIN, "t-gate", entry["superstep_id"], datetime.now(UTC) + timedelta(days=90)
            )
            is True
        )
        assert await fake_park_redis.ttl(f"agent:park:{_CHAIN}") > before
        assert await fake_park_redis.ttl(f"agent:park:step:t-gate:{entry['superstep_id']}") > before

    asyncio.run(go())


def test_detaching_a_dead_chain_leaves_a_benign_tombstone(fake_park_redis: Any) -> None:
    async def go() -> None:
        await idx.detach_chained_parks([_CHAIN])
        entry = await idx.read_park_entry(_CHAIN)
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_detaching_never_overwrites_a_live_park(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A key that DOES hold a park (a concurrent re-drive that reached the persist first) is
        # left exactly as it is — the detach is written NX, so it can only fill an empty slot.
        await drv.persist_park(_park_identity(None), [("int1", {_CHAIN: None})])
        await idx.detach_chained_parks([_CHAIN])
        entry = await idx.read_park_entry(_CHAIN)
        assert entry is not None
        assert not idx.is_resolved_tombstone(entry)
        assert entry["agent_name"] == "langchain_deep_agent"

    asyncio.run(go())


def test_park_persist_records_multiple_parks_as_one_superstep(fake_park_redis: Any) -> None:
    async def go() -> None:
        # Two distinct park interrupts (parallel subagent parks) persist into ONE super-step:
        # each entry carries ITS interaction's own interrupt, both share the super-step id, and
        # the barrier covers the union.
        # A keep-forever (None) bound lets the None-expiry parks pass the retention gate.
        parks = [("intA", {"iA": None}), ("intB", {"iB": None})]
        await drv.persist_park(_park_identity(None), parks)
        entry_a = await idx.read_park_entry("iA")
        entry_b = await idx.read_park_entry("iB")
        assert entry_a is not None
        assert entry_b is not None
        assert entry_a["interrupt_id"] == "intA"
        assert entry_b["interrupt_id"] == "intB"
        assert entry_a["superstep_id"] == entry_b["superstep_id"]
        superstep_id = idx.compute_superstep_id(["iA", "iB"])
        assert entry_a["superstep_id"] == superstep_id
        barrier = await idx.read_barrier("t-gate", superstep_id)
        assert barrier is not None
        assert set(barrier["expected"]) == {"iA", "iB"}

    asyncio.run(go())


def test_park_persist_allows_any_expiry_under_postgres_keep_forever(fake_park_redis: Any) -> None:
    async def go() -> None:
        # A keep-forever run (postgres checkpoint) computes a ``None`` retention bound, so any
        # deadline passes — the generalized gate reads the bound off the identity, not a provider.
        interactions: dict[str, Any] = {"i1": _iso_in(10_000_000), "i2": None}
        await drv.persist_park(_park_identity(None), [("int1", interactions)])
        assert await idx.read_park_entry("i1") is not None
        assert await idx.read_park_entry("i2") is not None

    asyncio.run(go())


def test_persist_superstep_is_atomic_all_or_nothing(fake_park_redis: Any) -> None:
    async def go() -> None:
        entries: dict[str, dict[str, Any]] = {
            "iA": {"agent_name": "langchain_deep_agent", "thread_id": "t", "superstep_id": "s"},
            "iB": {"agent_name": "langchain_deep_agent", "thread_id": "t", "superstep_id": "s"},
        }
        expected: dict[str, Any] = {"iA": None, "iB": None}

        # A crash before the single EXEC flushes: every entry and the barrier are buffered in
        # the pipeline, never written incrementally, so the post-state is ABSENT — no partial set.
        real_pipeline = fake_park_redis.pipeline

        def crashing_pipeline(*args: Any, **kwargs: Any) -> Any:
            pipe = real_pipeline(*args, **kwargs)

            async def boom() -> None:
                raise RuntimeError("crash before EXEC")

            pipe.execute = boom
            return pipe

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(fake_park_redis, "pipeline", crashing_pipeline)
            with pytest.raises(RuntimeError, match="crash before EXEC"):
                await idx.persist_superstep(entries, "t", "s", expected, {"iA": None, "iB": None}, 100)
        assert await idx.read_park_entry("iA") is None
        assert await idx.read_park_entry("iB") is None
        assert await idx.read_barrier("t", "s") is None

        # A clean persist flushes the whole set in one EXEC: every entry AND the barrier land.
        await idx.persist_superstep(entries, "t", "s", expected, {"iA": None, "iB": None}, 100)
        assert await idx.read_park_entry("iA") is not None
        assert await idx.read_park_entry("iB") is not None
        barrier = await idx.read_barrier("t", "s")
        assert barrier is not None
        assert set(barrier["expected"]) == {"iA", "iB"}

    asyncio.run(go())


def test_park_entry_ttl_scales_to_the_ask_deadline(fake_park_redis: Any) -> None:
    async def go() -> None:
        # Keep-forever retention (postgres) lets a far-future deadline persist. The entry TTL must
        # scale to that deadline, not TTL out at the 30-day floor while the barrier and checkpoint
        # survive to it — else a valid in-window answer would find no entry and storm to give-up.
        far = (datetime.now(UTC) + timedelta(days=40)).isoformat()
        near = (datetime.now(UTC) + timedelta(minutes=5)).isoformat()
        await drv.persist_park(_park_identity(None), [("int1", {"i_far": far, "i_near": near})])

        margin = idx._BARRIER_TTL_MARGIN_SECONDS
        far_ttl = await fake_park_redis.ttl(idx._park_key("i_far"))
        near_ttl = await fake_park_redis.ttl(idx._park_key("i_near"))
        # The far entry outlasts its 40-day deadline plus the margin — above the 30-day floor.
        assert far_ttl > idx._PARK_ENTRY_TTL_SECONDS
        assert far_ttl >= 40 * 24 * 60 * 60 + margin - 5
        # A short-horizon entry keeps the 30-day backstop floor.
        assert near_ttl == idx._PARK_ENTRY_TTL_SECONDS
        # The barrier floors at or above every entry it coordinates.
        superstep_id = idx.compute_superstep_id(["i_far", "i_near"])
        barrier_ttl = await fake_park_redis.ttl(idx._barrier_key("t-gate", superstep_id))
        assert barrier_ttl >= far_ttl

    asyncio.run(go())


# ---- agent_resume driver --------------------------------------------------


async def _write_park(entry_ids: list[str], interrupt_id: str = "int1", thread_id: str = "t") -> str:
    superstep_id = idx.compute_superstep_id(entry_ids)
    entries = {
        interaction_id: {
            "agent_name": "langchain_deep_agent",
            "thread_id": thread_id,
            "superstep_id": superstep_id,
            "interrupt_id": interrupt_id,
            # Engine facts (checkpoint provider, recursion limit) ride inside rebuild_kwargs,
            # never top-level entry fields, on the provider-free index.
            "rebuild_kwargs": {"checkpoint_provider": "redis", "recursion_limit": 50},
        }
        for interaction_id in entry_ids
    }
    expected: dict[str, Any] = dict.fromkeys(entry_ids)
    await idx.persist_superstep(entries, thread_id, superstep_id, expected, dict.fromkeys(entry_ids), 100)
    return superstep_id


def test_agent_resume_buffers_until_all_siblings_answered(fake_park_redis: Any) -> None:
    async def go() -> None:
        await _write_park(["iA", "iB"])
        out = await agent_resume("iA", "answer-a")
        assert out == {"status": "buffered", "remaining": 1}
        # The still-pending sibling keeps the park entries in place.
        assert await idx.read_park_entry("iA") is not None
        assert await idx.read_park_entry("iB") is not None

    asyncio.run(go())


def test_agent_resume_raises_on_lost_drive_lease(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        # Another live worker already holds the drive lease.
        assert await idx.try_claim_drive("t", superstep_id, "other-worker")
        with pytest.raises(AgentResumeDriveInProgressError):
            await agent_resume("i1", "the answer")
        # The park index is LEFT intact so the platform's reaper redelivers (H1).
        assert await idx.read_park_entry("i1") is not None

    asyncio.run(go())


def test_agent_resume_raises_on_missing_park_entry(fake_park_redis: Any) -> None:
    async def go() -> None:
        with pytest.raises(AgentResumeParkEntryNotFoundError):
            await agent_resume("nope", "x")

    asyncio.run(go())


# ---- finalize_drive raise branch ----------------------------------------------------------


def test_finalize_drive_raises_on_a_park_with_no_identity_bound(fake_park_redis: Any) -> None:
    from tai42_agents._internal.park.driver import finalize_drive
    from tai42_agents._internal.park.middleware import AGENT_PARK_PAYLOAD_KEY

    class _Interrupt:
        def __init__(self, id: str, value: Any) -> None:
            self.id = id
            self.value = value

    class _Task:
        def __init__(self, interrupts: list[Any]) -> None:
            self.interrupts = interrupts
            self.state = None

    class _Snapshot:
        def __init__(self, tasks: list[Any]) -> None:
            self.tasks = tasks

    class _FakeAgent:
        def __init__(self, snapshot: Any) -> None:
            self._snapshot = snapshot

        async def aget_state(self, config: Any, subgraphs: bool = False) -> Any:
            return self._snapshot

    park_value = {AGENT_PARK_PAYLOAD_KEY: {"interactions": {"i1": None}}}
    agent = _FakeAgent(_Snapshot([_Task([_Interrupt("int1", park_value)])]))

    async def go() -> None:
        # A park interrupt surfaced but no park identity is bound (interrupt_on forces the state
        # read): the run parked with no durable resume path, so finalize raises loudly.
        with pytest.raises(AgentParkNotHostableError, match="no park identity bound"):
            await finalize_drive(agent, {}, {"approve": True}, None)

    asyncio.run(go())


# ---- drive-lease behavior (token-checked claim / renew / release, TTL reclaim) ------------


def test_drive_lease_release_lets_a_fresh_claim_win(fake_park_redis: Any) -> None:
    async def go() -> None:
        assert await idx.try_claim_drive("t", "s", "tok1")
        # A second claim while the lease is live loses.
        assert not await idx.try_claim_drive("t", "s", "tok2")
        # The holder releases; a fresh claim now wins.
        await idx.release_claim("t", "s", "tok1")
        assert await idx.try_claim_drive("t", "s", "tok2")

    asyncio.run(go())


def test_drive_lease_release_with_wrong_token_leaves_the_lease(fake_park_redis: Any) -> None:
    async def go() -> None:
        assert await idx.try_claim_drive("t", "s", "tok1")
        # A stale holder's release names the wrong token, so the live lease is untouched.
        await idx.release_claim("t", "s", "wrong")
        assert not await idx.try_claim_drive("t", "s", "tok2")

    asyncio.run(go())


def test_drive_lease_renew_only_extends_for_the_holder(fake_park_redis: Any) -> None:
    async def go() -> None:
        assert await idx.try_claim_drive("t", "s", "tok1")
        # The holder renews; a stale token does not.
        assert await idx.renew_claim("t", "s", "tok1")
        assert not await idx.renew_claim("t", "s", "wrong")
        # A renew of an absent lease is False.
        assert not await idx.renew_claim("t", "absent", "tok1")

    asyncio.run(go())


def test_drive_lease_expiry_reclaims_with_no_manual_delete(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(idx, "_DRIVE_LEASE_SECONDS", 1)

    async def go() -> None:
        assert await idx.try_claim_drive("t", "s", "tok1")
        assert not await idx.try_claim_drive("t", "s", "tok2")
        # A crashed winner's lease simply expires — a later reclaim wins with no manual delete.
        await asyncio.sleep(1.2)
        assert await idx.try_claim_drive("t", "s", "tok2")

    asyncio.run(go())


# ---- buffer_answer redelivery idempotency -------------------------------------------------


def test_buffer_answer_is_idempotent_across_redeliveries(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["iA", "iB"])
        present, total = await idx.buffer_answer("t", superstep_id, "iA", "answer-a")
        assert (present, total) == (1, 2)
        # A redelivered answer for the SAME interaction is a no-op — present stays stable.
        present, total = await idx.buffer_answer("t", superstep_id, "iA", "answer-a-again")
        assert (present, total) == (1, 2)
        # The buffered value is the FIRST answer (HSETNX never overwrites).
        barrier = await idx.read_barrier("t", superstep_id)
        assert barrier is not None
        assert barrier["outputs"]["iA"] == "answer-a"

    asyncio.run(go())


def test_agent_resume_on_resolved_tombstone_returns_already_resolved(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        # The super-step already drove cleanly: its entry is a resolved tombstone.
        await idx.finalize_resolved_superstep("t", superstep_id, ["i1"])
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)
        # A lapped redelivery of an orphaned due-record clears benignly — no raise, a benign shape.
        out = await agent_resume("i1", "the answer")
        assert out == {"status": "already_resolved"}

    asyncio.run(go())


def test_two_completers_race_loser_then_already_resolved_on_redelivery(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["i1"])
        # Worker A won the barrier and holds a live drive lease.
        assert await idx.try_claim_drive("t", superstep_id, "worker-A")
        # Worker B's redelivery completes the barrier but loses the drive: it raises so the
        # platform retains its durable retry ticket.
        with pytest.raises(AgentResumeDriveInProgressError):
            await agent_resume("i1", "the answer")
        assert await idx.read_park_entry("i1") is not None

        # Worker A finishes and finalizes the super-step to a tombstone.
        await idx.finalize_resolved_superstep("t", superstep_id, ["i1"])
        # Worker B redelivers again and now clears benignly on the tombstone.
        out = await agent_resume("i1", "the answer")
        assert out == {"status": "already_resolved"}

    asyncio.run(go())


def test_finalize_resolved_superstep_is_atomic_single_batch(fake_park_redis: Any) -> None:
    async def go() -> None:
        superstep_id = await _write_park(["iA", "iB"])
        assert await idx.try_claim_drive("t", superstep_id, "winner")
        # Pre-state: two live park entries, a barrier, and a claim lease.
        assert await idx.read_park_entry("iA") is not None
        assert await idx.read_park_entry("iB") is not None
        assert await idx.read_barrier("t", superstep_id) is not None

        await idx.finalize_resolved_superstep("t", superstep_id, ["iA", "iB"])

        # Post-state after finalize: EVERY entry is a resolved tombstone, and the barrier and
        # claim lease are both gone.
        for interaction_id in ("iA", "iB"):
            entry = await idx.read_park_entry(interaction_id)
            assert entry is not None
            assert idx.is_resolved_tombstone(entry)
        assert await idx.read_barrier("t", superstep_id) is None
        assert await fake_park_redis.get(idx._claim_key("t", superstep_id)) is None

    asyncio.run(go())


# ---- full cross-worker cycle ----------------------------------------------


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
    # and this full-cycle test's synthetic None-expiry park passes it.
    checkpoint_ttl_minutes = None
    store = "memory"
    store_conn_string = None


class _LlmSettings:
    def with_fallbacks(self, kwargs: Any) -> dict[str, Any]:
        return {}


def _wire_real_build(
    monkeypatch: pytest.MonkeyPatch, model: BaseChatModel, saver: InMemorySaver, store: InMemoryStore
) -> None:
    """Keep the REAL build_langchain_deep_agent but inject the scripted model + a SHARED
    checkpointer/store, so the park run and its resume read one durable checkpoint."""

    async def fake_get_llm_async(provider: str, **kwargs: Any) -> Any:
        return model

    monkeypatch.setattr(agent_mod, "get_llm_async", fake_get_llm_async)
    monkeypatch.setattr(agent_mod, "checkpoint_registry", lambda: _Registry(saver))
    monkeypatch.setattr(agent_mod, "store_registry", lambda: _Registry(store))
    monkeypatch.setattr(agent_mod, "llm_provider_settings", _ProviderSettings)
    # The park-persist gate reads the checkpoint retention through the driver's own
    # ``llm_provider_settings``; point it at the same keep-forever double.
    monkeypatch.setattr(drv, "llm_provider_settings", _ProviderSettings)
    monkeypatch.setattr(agent_mod, "llm_settings", _LlmSettings)
    # The recording app's monitoring writer hands back string callback sentinels; keep
    # them out of the run config (the config already carries the pinned thread_id).
    monkeypatch.setattr(agent_mod, "init_langgraph_config", lambda config=None: dict(config or {}))


def test_full_park_resume_cycle_runs_ask_once_and_clears_index(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    store = InMemoryStore()
    ask = _CountingAsk("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_real_build(monkeypatch, model, saver, store)
    app_tools.client_tools["ask"] = ask.tool()

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> Any:
        receipt = await agent.run(tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-int")
        assert receipt == {
            "status": "suspended",
            "interaction_ids": ["i1"],
            "thread_id": "t-int",
            "expiry_at": _WITHIN_HORIZON_ISO,
        }
        assert ask.calls == 1
        assert await idx.read_park_entry("i1") is not None

        # Resume on a fresh runtime: same shared saver stands in for the durable checkpoint.
        result = await agent_resume("i1", "the answer")
        assert result == "all done"
        # The parked tool ran exactly once — the resume substituted the answer, never re-ran it.
        assert ask.calls == 1
        # A clean drive finalizes the park entry to a resolved tombstone (not an absent key), so
        # a lapped redelivery clears benignly instead of storming on a vanished entry.
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        assert idx.is_resolved_tombstone(entry)

    asyncio.run(go())


def test_agent_resume_rejects_a_no_longer_pending_interrupt(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    saver = InMemorySaver()
    store = InMemoryStore()
    ask = _CountingAsk("i1")
    model = ScriptedChatModel([_ask_call(), AIMessage(content="all done")])
    _wire_real_build(monkeypatch, model, saver, store)
    app_tools.client_tools["ask"] = ask.tool()

    agent = tai42_app.agents.get_agent("langchain_deep_agent")

    async def go() -> None:
        await agent.run(tool_names=["ask"], checkpoint_provider="redis", user_message="go", thread_id="t-stale")

        # Corrupt the stored interrupt id so it no longer matches the pending park interrupt.
        entry = await idx.read_park_entry("i1")
        assert entry is not None
        entry["interrupt_id"] = "bogus-interrupt-id"
        await idx.persist_superstep(
            {"i1": entry}, entry["thread_id"], entry["superstep_id"], {"i1": None}, {"i1": None}, 100
        )

        with pytest.raises(AgentResumeInterruptNotPendingError):
            await agent_resume("i1", "the answer")
        # A rejected drive leaves the index for the reaper (never a silent clear).
        assert await idx.read_park_entry("i1") is not None

    asyncio.run(go())


# ---- M=2 parallel-subagent park driven to completion through agent_resume --


class _EchoingScriptedModel(BaseChatModel):
    """Like ``ScriptedChatModel`` but a scripted response may be a callable ``(messages) ->
    AIMessage``, so a finalize turn can ECHO the answer that was substituted into the last
    tool result. This is how a resumed subagent's output is made to depend on ITS OWN
    answer, proving each answer landed on its own interrupt rather than crossing over."""

    _responses: list[BaseMessage | Callable[[list[BaseMessage]], BaseMessage]] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(
        self, responses: Sequence[BaseMessage | Callable[[list[BaseMessage]], BaseMessage]], **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._responses = list(responses)

    @property
    def _llm_type(self) -> str:
        return "echoing-scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _EchoingScriptedModel:
        return self

    def _generate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kwargs: Any
    ) -> ChatResult:
        response = self._responses[self._index]
        self._index += 1
        message: BaseMessage = response(messages) if callable(response) else response
        return ChatResult(generations=[ChatGeneration(message=message)])


def _ask_for_subagent(messages: list[BaseMessage]) -> AIMessage:
    """A subagent's ask turn: read the task description (``A`` / ``B``) this subagent was
    launched with and pass it to ``ask``, so subagent A always parks ``iA`` and subagent B
    always parks ``iB``. This binds each subagent to a FIXED interaction independently of the
    non-deterministic order the two parallel subagents reach the model, so the terminal
    ``task_id -> answer`` mapping is stable and a crossover cannot hide behind a scheduling flip."""
    who = next(str(m.content) for m in messages if isinstance(m, HumanMessage))
    return AIMessage(content="", tool_calls=[{"id": f"c{who}", "name": "ask", "args": {"who": who}}])


def _echo_last_tool_result(messages: list[BaseMessage]) -> AIMessage:
    """A subagent's finalize: echo the content of the last tool result — the answer just
    substituted into its own ``ask`` interrupt."""
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    return AIMessage(content=str(tool_messages[-1].content))


def _echo_task_results(messages: list[BaseMessage]) -> AIMessage:
    """The main agent's finalize: surface each subagent task's result keyed by ITS task id in a
    fixed ``ta`` then ``tb`` order, so the terminal output pins which subagent carried which
    answer. A crossover that fed one interrupt the other's answer would swap the two task
    results and flip the output, so the per-task assertion fails — proving both designated
    answers arrived, each exactly once, with no crossover between the interrupts."""
    by_task = {
        m.tool_call_id: str(m.content)
        for m in messages
        if isinstance(m, ToolMessage) and m.tool_call_id in {"ta", "tb"}
    }
    return AIMessage(content=f"ta={by_task['ta']};tb={by_task['tb']}")


def _two_parallel_subagent_park_setup(
    monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> tuple[Any, Any, dict[str, int]]:
    """Wire (no async yet) a REAL deep-agent run whose main agent calls one ``asker`` subagent
    twice in parallel; each subagent async-asks and parks, surfacing two distinct interrupts in
    one super-step. Subagent A (task ``ta``, description ``A``) parks ``iA`` and subagent B
    (task ``tb``, description ``B``) parks ``iB`` — a FIXED binding, so ``ta`` always carries
    iA's answer and ``tb`` iB's regardless of parallel scheduling order. Each answer is
    self-identifying (``for-iA`` / ``for-iB``) and finalize turns echo the answer their own
    ``ask`` interrupt received, so the terminal output carries both designated answers only
    when each reached the interrupt it was addressed to."""
    saver = InMemorySaver()
    store = InMemoryStore()
    ask_calls = {"n": 0}
    id_for_who = {"A": "iA", "B": "iB"}

    def ask(who: str) -> dict[str, Any]:
        ask_calls["n"] += 1
        return suspended_interaction_marker(id_for_who[who], _WITHIN_HORIZON, get_resume_continuation_tool())

    app_tools.client_tools["ask"] = StructuredTool.from_function(ask, name="ask", description="Ask the user and park.")

    model = _EchoingScriptedModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {"id": "ta", "name": "task", "args": {"description": "A", "subagent_type": "asker"}},
                    {"id": "tb", "name": "task", "args": {"description": "B", "subagent_type": "asker"}},
                ],
            ),
            _ask_for_subagent,  # a subagent's ask turn — parks iA or iB by its own description
            _ask_for_subagent,  # the other subagent's ask turn
            _echo_last_tool_result,  # subagent that resumes first finalizes, echoing its own answer
            _echo_last_tool_result,  # the other subagent finalizes, echoing its own answer
            _echo_task_results,  # main agent finalizes over both task results
        ]
    )
    _wire_real_build(monkeypatch, model, saver, store)

    subagent = DeepSubAgentSpec(name="asker", description="asks the user", system_prompt="ask", tools=["ask"])
    agent = tai42_app.agents.get_agent("langchain_deep_agent")
    assert isinstance(agent, agent_mod.DeepAgent)
    return agent, subagent, ask_calls


async def _park_two_parallel_subagents(agent: Any, subagent: Any, ask_calls: dict[str, int], thread_id: str) -> None:
    """Drive the parking turn: the main agent calls the ``asker`` subagent twice in parallel,
    each async-asks and parks, surfacing two interrupts in one super-step. The whole cycle
    (this park plus the caller's resume) runs in ONE event loop so the shared fakeredis
    connection never crosses loops."""
    receipt = await agent.run(
        subagents=[subagent],
        checkpoint_provider="redis",
        user_message="go",
        thread_id=thread_id,
    )
    assert receipt["status"] == "suspended"
    assert set(receipt["interaction_ids"]) == {"iA", "iB"}
    # Each subagent's ask ran exactly once — two parks, one interaction apiece.
    assert ask_calls["n"] == 2
    assert await idx.read_park_entry("iA") is not None
    assert await idx.read_park_entry("iB") is not None


def test_two_parallel_subagent_parks_resume_iA_first(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    thread_id = "t-multipark-a"
    agent, subagent, ask_calls = _two_parallel_subagent_park_setup(monkeypatch, app_tools)

    async def go() -> None:
        await _park_two_parallel_subagents(agent, subagent, ask_calls, thread_id)
        # Answer iA first: it buffers, no drive yet (its sibling is still outstanding).
        assert await agent_resume("iA", "for-iA") == {"status": "buffered", "remaining": 1}
        # Answering iB completes the barrier and drives the whole super-step to ONE terminal.
        result = await agent_resume("iB", "for-iB")
        # Each task echoed its OWN subagent's answer — ``ta`` carried iA's, ``tb`` carried iB's.
        # A swap would flip these, so this pins answer→interrupt routing: no crossover.
        assert result == "ta=for-iA;tb=for-iB"
        # No ask re-ran on resume — the answers were substituted, never re-invoked.
        assert ask_calls["n"] == 2
        # Both entries tombstoned in one finalize; the barrier is gone (single terminal).
        for interaction_id in ("iA", "iB"):
            entry = await idx.read_park_entry(interaction_id)
            assert entry is not None
            assert idx.is_resolved_tombstone(entry)
        assert await idx.read_barrier(thread_id, idx.compute_superstep_id(["iA", "iB"])) is None

    asyncio.run(go())


def test_two_parallel_subagent_parks_resume_iB_first(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, app_tools: Any
) -> None:
    thread_id = "t-multipark-b"
    agent, subagent, ask_calls = _two_parallel_subagent_park_setup(monkeypatch, app_tools)

    async def go() -> None:
        await _park_two_parallel_subagents(agent, subagent, ask_calls, thread_id)
        # Reverse the answer order: iB first buffers, iA completes the barrier and drives.
        assert await agent_resume("iB", "for-iB") == {"status": "buffered", "remaining": 1}
        result = await agent_resume("iA", "for-iA")
        # Answer order does not change routing — ``ta`` still carries iA's answer and ``tb`` iB's;
        # each answer reached its own interrupt.
        assert result == "ta=for-iA;tb=for-iB"
        assert ask_calls["n"] == 2
        for interaction_id in ("iA", "iB"):
            entry = await idx.read_park_entry(interaction_id)
            assert entry is not None
            assert idx.is_resolved_tombstone(entry)
        assert await idx.read_barrier(thread_id, idx.compute_superstep_id(["iA", "iB"])) is None

    asyncio.run(go())
