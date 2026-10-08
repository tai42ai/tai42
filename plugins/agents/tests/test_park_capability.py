"""Park-capability gating: :func:`build_park_identity` captures a durable, rebuildable run and
refuses everything that could not be resumed on a fresh worker.

The park index is backed by an in-memory fakeredis routed in through the index module's
``client_ctx`` seam, and the capability module's park-Redis read is pointed at the same
configured-looking settings so a run is judged park-capable.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis

from tai42_agents._internal.park import build_park_identity
from tai42_agents._internal.park import capability as cap
from tai42_agents._internal.park import index as idx


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
    monkeypatch.setattr(cap, "agents_park_redis_settings", lambda: settings)
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
    monkeypatch.setattr(cap, "agents_park_redis_settings", lambda: SimpleNamespace(redis_url=None))
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
def test_persist_adds_the_superstep_to_the_thread_live_set_and_finalize_removes_it(
    fake_park_redis: Any,
) -> None:
    async def _go() -> None:
        await idx.persist_superstep(
            {"i1": {"x": 1}}, "thread-a", "step-1", {"i1": "int-1"}, {"i1": None}, barrier_ttl_seconds=600
        )
        assert await fake_park_redis.smembers("agent:park:live:thread-a") == {"step-1"}
        assert 0 < await fake_park_redis.ttl("agent:park:live:thread-a") <= 600
        await fake_park_redis.set("agent:park:step:thread-a:step-1:claim", "tok")
        await idx.finalize_resolved_superstep(
            "thread-a", "step-1", ["i1"], resolution="terminal", value="done", token="tok"
        )
        assert await fake_park_redis.smembers("agent:park:live:thread-a") == set()

    asyncio.run(_go())


def test_the_live_set_ttl_only_grows(fake_park_redis: Any) -> None:
    async def _go() -> None:
        await idx.persist_superstep({"i1": {}}, "thread-b", "step-1", {}, {"i1": None}, barrier_ttl_seconds=900)
        await idx.persist_superstep({"i2": {}}, "thread-b", "step-2", {}, {"i2": None}, barrier_ttl_seconds=300)
        assert await fake_park_redis.ttl("agent:park:live:thread-b") > 800
        assert await fake_park_redis.smembers("agent:park:live:thread-b") == {"step-1", "step-2"}

    asyncio.run(_go())


def test_the_filter_claims_a_thread_with_a_live_barrier_only(fake_park_redis: Any) -> None:
    async def _go() -> None:
        from tai42_kit.llm.checkpoint import live_thread_filters

        await idx.persist_superstep({"i1": {}}, "live", "s1", {}, {"i1": None}, barrier_ttl_seconds=600)
        await idx.persist_superstep({"i2": {}}, "expired", "s2", {}, {"i2": None}, barrier_ttl_seconds=600)
        await fake_park_redis.delete("agent:park:step:expired:s2")  # its barrier expired
        await idx.persist_superstep({"i3": {}}, "finalized", "s3", {}, {"i3": None}, barrier_ttl_seconds=600)
        await fake_park_redis.set("agent:park:step:finalized:s3:claim", "tok")
        await idx.finalize_resolved_superstep("finalized", "s3", ["i3"], resolution="terminal", value=None, token="tok")

        claimed = await idx.threads_with_live_barriers("redis", None, ["live", "expired", "finalized", "never"])
        assert claimed == {"live"}
        assert live_thread_filters()["agents"] is idx.threads_with_live_barriers

    asyncio.run(_go())


def test_the_filter_claims_nothing_without_a_park_index(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _go() -> None:
        monkeypatch.setattr(idx, "agents_park_redis_settings", lambda: SimpleNamespace(redis_url=None))
        assert await idx.threads_with_live_barriers("redis", None, ["t"]) == set()

    asyncio.run(_go())
