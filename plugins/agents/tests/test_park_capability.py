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
from tests.conftest import APP, bind_park_index

from tai42_agents._internal.park import build_park_identity, persist_park
from tai42_agents._internal.park import capability as cap
from tai42_agents._internal.park.errors import ParkExpiryExceedsRetentionError
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


@pytest.fixture
def durable_store() -> Any:
    from tai42_kit.llm.checkpoint import (
        CheckpointProvider,
        CheckpointProviderFacts,
        providers,
        register_checkpoint_provider,
    )

    class DurableStore(CheckpointProvider):
        name = "plugin-durable-store"
        facts = CheckpointProviderFacts(durable=True, retention="sweep", channel_storage="per_version")

        async def open(self, conn_string: str | None) -> Any:
            raise NotImplementedError

        def saver(self, resource: Any) -> Any:
            raise NotImplementedError

        def value_encoding(self, saver: Any, compress_threshold: int | None) -> Any:
            raise NotImplementedError

        async def recorded_generation(self, resource: Any) -> int | None:
            raise NotImplementedError

        async def record_generation(self, resource: Any, generation: int) -> None:
            raise NotImplementedError

        async def holds_checkpoints(self, resource: Any) -> bool:
            raise NotImplementedError

    held = dict(providers._PROVIDERS._generation.committed())
    instances = dict(providers._instances)
    register_checkpoint_provider(DurableStore)
    yield DurableStore.name
    committed = providers._PROVIDERS._generation.committed()
    committed.clear()
    committed.update(held)
    providers._instances.clear()
    providers._instances.update(instances)


def test_a_durable_store_registered_by_a_plugin_is_park_capable(fake_park_redis: Any, durable_store: str) -> None:
    park = build_park_identity(
        agent_name="langchain_deep_agent",
        config={"configurable": {"thread_id": "t1"}},
        checkpoint_provider=durable_store,
        has_live_tools=False,
        rebuild_kwargs={},
        recursion_limit=50,
        bind=True,
    )
    assert park is not None
    assert park.rebuild_kwargs["checkpoint_provider"] == durable_store
    assert park.retention_bound is not None


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
# The retention bound is the kit's park horizon on every durable provider, less the
# window the platform may still deliver an answer after the ask's deadline
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("provider", ["redis", "postgres"])
def test_retention_bound_is_the_waiting_horizon_less_the_redelivery_horizon(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    from datetime import UTC, datetime, timedelta

    from tai42_kit.settings import reset_all_settings

    APP.interactions.redelivery_horizon = 3600
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
    keep = timedelta(minutes=2880) - timedelta(seconds=3600)
    assert before + keep <= park.retention_bound <= after + keep


def _redis_park(thread_id: str) -> Any:
    park = build_park_identity(
        agent_name="tools_agent",
        config={"configurable": {"thread_id": thread_id}},
        checkpoint_provider="redis",
        has_live_tools=False,
        rebuild_kwargs={},
        recursion_limit=10,
        bind=True,
    )
    assert park is not None
    return park


def test_an_ask_answered_late_inside_the_redelivery_horizon_still_finds_its_checkpoint(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The platform may deliver an ask's answer up to the redelivery horizon after its deadline, and
    # the checkpoint is kept the waiting retention after its write. An ask whose deadline fits the
    # retention but whose late delivery does not is refused before any index key is written.
    from datetime import UTC, datetime, timedelta

    from tai42_kit.settings import reset_all_settings

    APP.interactions.redelivery_horizon = 3600
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "2880")
    reset_all_settings()

    async def go() -> None:
        retention = timedelta(minutes=2880)
        late = (datetime.now(UTC) + retention - timedelta(minutes=30)).isoformat()
        with pytest.raises(ParkExpiryExceedsRetentionError) as excinfo:
            await persist_park(_redis_park("t-late"), [("int1", {"i-late": late})])
        assert excinfo.value.interaction_id == "i-late"
        assert await agents_park_index().read_entry("i-late") is None

        fits = (datetime.now(UTC) + retention - timedelta(minutes=90)).isoformat()
        await persist_park(_redis_park("t-fits"), [("int1", {"i-fits": fits})])
        assert await agents_park_index().read_entry("i-fits") is not None

    try:
        asyncio.run(go())
    finally:
        reset_all_settings()


def test_a_chained_park_clamped_to_the_bound_is_answerable_through_the_redelivery_horizon(
    fake_park_redis: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A chained key's inherited deadline clamps to the retention bound; a nested answer delivered up
    # to the redelivery horizon after that deadline still lands inside the checkpoint's retention.
    from datetime import UTC, datetime, timedelta

    from tai42_kit.settings import reset_all_settings

    APP.interactions.redelivery_horizon = 3600
    monkeypatch.setenv("LLM_PROVIDER_CHECKPOINT_RETENTION_WAITING_MINUTES", "2880")
    reset_all_settings()
    chain = "tai42:chained-park:k-clamp"

    async def go() -> None:
        before = datetime.now(UTC)
        far = (before + timedelta(days=90)).isoformat()
        written = await persist_park(_redis_park("t-chain"), [("int1", {chain: far})])
        deadline = datetime.fromisoformat(written[chain])
        assert deadline + timedelta(seconds=3600) <= datetime.now(UTC) + timedelta(minutes=2880)

    try:
        asyncio.run(go())
    finally:
        reset_all_settings()


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
