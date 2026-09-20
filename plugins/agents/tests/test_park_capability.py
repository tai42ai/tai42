"""Park-capability gating: :func:`build_park_identity` captures a durable, rebuildable run and
refuses everything that could not be resumed on a fresh worker.

The park index is backed by an in-memory fakeredis routed in through the index module's
``client_ctx`` seam, and the capability module's park-Redis read is pointed at the same
configured-looking settings so a run is judged park-capable.
"""

from __future__ import annotations

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
