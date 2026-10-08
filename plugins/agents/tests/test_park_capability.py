"""Park-capability gating: :func:`build_park_identity` captures a durable, rebuildable run and
refuses everything that could not be resumed on a fresh worker.

The agents' park index is bound to an in-memory fakeredis and reported configured, so a run is
judged park-capable.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fakeredis import aioredis
from tai42_kit.interactions.park_index import superstep_id
from tests.conftest import bind_park_index

from tai42_agents._internal.park import build_park_identity
from tai42_agents._internal.park import capability as cap
from tai42_agents._internal.park.park_binding import agents_park_index, threads_with_live_barriers


@pytest.fixture
def fake_park_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    """Route the park index at a shared in-memory fakeredis and report the park Redis as
    configured (so a run is judged park-capable)."""
    redis = aioredis.FakeRedis(decode_responses=True)
    bind_park_index(monkeypatch, redis)
    return redis


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
    bind_park_index(monkeypatch, aioredis.FakeRedis(decode_responses=True), configured=False)
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


# --------------------------------------------------------------------------- #
# The retention bound is the kit's park horizon on every durable provider
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider", ["redis", "postgres"])
def test_retention_bound_is_the_waiting_horizon_of_a_durable_provider(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from datetime import UTC, datetime, timedelta

    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "2880")
    reset_all_settings()
    try:
        before = datetime.now(UTC)
        park = build_park_identity(
            agent_name="tools_agent",
            config={"configurable": {"thread_id": "t1"}},
            checkpoint_provider=provider,
            has_live_tools=False,
            rebuild_kwargs={},
            recursion_limit=10,
            bind=True,
        )
        after = datetime.now(UTC)
    finally:
        reset_all_settings()
    assert park is not None
    assert park.retention_bound is not None
    assert before + timedelta(minutes=2880) <= park.retention_bound <= after + timedelta(minutes=2880)


def test_retention_horizon_refuses_a_provider_without_one() -> None:
    with pytest.raises(RuntimeError, match="unexpected checkpoint provider 'memory' at park-persist time"):
        cap._checkpoint_retention_horizon("memory")


# --------------------------------------------------------------------------- #
# The per-thread live-barrier set and the agents live-thread filter
# --------------------------------------------------------------------------- #
async def _persist(thread_id: str, member: str) -> str:
    step = superstep_id([member])
    await agents_park_index().persist(
        thread_id=thread_id,
        superstep=step,
        entries={member: {}},
        expected={member: None},
        entry_ttl={member: 600},
        barrier_ttl=600,
    )
    return step


def test_the_filter_claims_a_thread_with_a_live_barrier_only(fake_park_redis: Any) -> None:
    async def _go() -> None:
        from tai42_kit.llm.checkpoint import live_thread_filters

        index = agents_park_index()
        await _persist("live", "i1")
        expired = await _persist("expired", "i2")
        await fake_park_redis.delete(index.barrier_key("expired", expired))  # its barrier expired
        finalized = await _persist("finalized", "i3")
        async with index.claim("finalized", finalized) as lease:
            await index.finalize(lease, member_ids=["i3"], resolution="terminal", value=None)

        claimed = await threads_with_live_barriers("redis", None, ["live", "expired", "finalized", "never"])
        assert claimed == {"live"}
        assert live_thread_filters()["agents"] is threads_with_live_barriers

    asyncio.run(_go())


def test_the_filter_claims_nothing_without_a_park_index(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _go() -> None:
        bind_park_index(monkeypatch, aioredis.FakeRedis(decode_responses=True), configured=False)
        assert await threads_with_live_barriers("redis", None, ["t"]) == set()

    asyncio.run(_go())
