"""The provider-free park generalization: shared workspace key/lease and the spec-builder.

Covers the engine-neutral pieces both durable-workspace engines (``claude_code`` and
``langchain_deep_agent``) share — the agent-namespaced workspace key, the cross-worker
per-workspace lease, the policied spec-builder, and the structural park-capability gate —
independent of any concrete engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from fakeredis import aioredis
from tai42_contract.sandbox import SandboxPolicy
from tai42_contract.sandbox.models import WORKSPACE_KEY_RE

from tai42_agents._internal import sandbox_util
from tai42_agents._internal.park import assert_park_capable, lease
from tai42_agents._internal.park import driver as drv
from tai42_agents._internal.park.errors import WorkspaceLeaseHeldError

# ---- workspace key derivation ---------------------------------------------


def test_workspace_key_for_is_namespaced_per_agent() -> None:
    # The same thread id under two engines never resolves to one volume.
    claude = sandbox_util.workspace_key_for("claude_code", "t-1")
    deep = sandbox_util.workspace_key_for("langchain_deep_agent", "t-1")
    assert claude != deep


def test_workspace_key_for_is_deterministic_and_charset_valid() -> None:
    key = sandbox_util.workspace_key_for("claude_code", "t-abc")
    assert key == sandbox_util.workspace_key_for("claude_code", "t-abc")
    # Valid under the provider workspace-key charset (embeddable in resource names).
    assert WORKSPACE_KEY_RE.fullmatch(key)
    assert re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key)


# ---- structural park-capability gate --------------------------------------


def _identity(*, bind: bool = True, rebuild_kwargs: dict[str, Any] | None = None) -> drv.ParkIdentity:
    return drv.ParkIdentity(
        agent_name="claude_code",
        thread_id="t-1",
        rebuild_kwargs={"a": 1} if rebuild_kwargs is None else rebuild_kwargs,
        bind=bind,
        retention_bound=None,
    )


def test_assert_park_capable_passes_a_capable_run() -> None:
    assert_park_capable(_identity(), durable=True, retention_bound=None)


def test_assert_park_capable_refuses_ephemeral() -> None:
    with pytest.raises(RuntimeError, match="ephemeral"):
        assert_park_capable(_identity(), durable=False, retention_bound=None)


def test_assert_park_capable_refuses_non_serializable_rebuild() -> None:
    with pytest.raises(RuntimeError, match="JSON-serializable"):
        assert_park_capable(_identity(rebuild_kwargs={"x": object()}), durable=True, retention_bound=None)


def test_assert_park_capable_refuses_unbound() -> None:
    with pytest.raises(RuntimeError, match="no resume continuation"):
        assert_park_capable(_identity(bind=False), durable=True, retention_bound=None)


# ---- provider-free identity ------------------------------------------------


def test_park_identity_carries_no_langgraph_facts() -> None:
    identity = _identity()
    assert set(drv.ParkIdentity.__slots__) == {
        "agent_name",
        "bind",
        "completion_context",
        "completion_tool",
        "execution_fingerprint",
        "execution_identity",
        "rebuild_kwargs",
        "retention_bound",
        "thread_id",
    }
    # The execution identity is a park-record fact (the authority a later out-of-band fire binds),
    # not a LangGraph engine fact — those stay out of the provider-free identity.
    assert not hasattr(identity, "checkpoint_provider")
    assert not hasattr(identity, "recursion_limit")


# ---- policied spec-builder -------------------------------------------------


def _stub_policy(monkeypatch: pytest.MonkeyPatch, policy: SandboxPolicy) -> None:
    monkeypatch.setattr(
        sandbox_util,
        "tai42_app",
        SimpleNamespace(sandboxes=SimpleNamespace(sandbox_policy=lambda: policy)),
    )


def test_build_policied_spec_defaults_network_to_platform_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = SandboxPolicy(egress="egress", isolation="container", scrub_transcript=False, durable=True)
    _stub_policy(monkeypatch, policy)
    spec, returned = sandbox_util.build_policied_spec(
        image="img@sha256:" + "a" * 64,
        workspace_key=sandbox_util.workspace_key_for("claude_code", "t-1"),
        durability="ephemeral",
        env={},
        ttl_seconds=100,
        labels={},
        network_setting=None,
    )
    # Unset per-agent setting → the platform egress posture; isolation LEFT unset for the floor.
    assert spec.network == "egress"
    assert spec.isolation is None
    assert returned is policy


def test_build_policied_spec_passes_a_narrowed_network_through(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = SandboxPolicy(egress="egress", isolation="container", scrub_transcript=True, durable=True)
    _stub_policy(monkeypatch, policy)
    spec, _ = sandbox_util.build_policied_spec(
        image="img@sha256:" + "b" * 64,
        workspace_key=sandbox_util.workspace_key_for("langchain_deep_agent", "t-2"),
        durability="persistent",
        env={},
        ttl_seconds=100,
        labels={"tai42.agent": "langchain_deep_agent"},
        network_setting="none",
    )
    assert spec.network == "none"
    assert spec.durability == "persistent"


# ---- cross-worker workspace lease -----------------------------------------


@pytest.fixture
def fake_lease_redis(monkeypatch: pytest.MonkeyPatch) -> aioredis.FakeRedis:
    redis = aioredis.FakeRedis(decode_responses=True)

    @contextlib.asynccontextmanager
    async def fake_client() -> AsyncIterator[Any]:
        yield redis

    monkeypatch.setattr(lease, "_lease_client", fake_client)
    monkeypatch.setattr(lease, "agents_park_redis_settings", lambda: SimpleNamespace(redis_url="redis://fake"))
    return redis


def test_workspace_lease_serializes_two_workers(fake_lease_redis: Any) -> None:
    async def go() -> None:
        async with lease.workspace_lease("ws-1", lease_ms=60_000):
            # A second worker naming the same workspace busy-errors while the first holds it.
            with pytest.raises(WorkspaceLeaseHeldError):
                async with lease.workspace_lease("ws-1", lease_ms=60_000):
                    pass
        # After the first releases (compare-and-delete), a later turn re-acquires.
        async with lease.workspace_lease("ws-1", lease_ms=60_000):
            pass

    asyncio.run(go())


def test_workspace_lease_release_is_token_checked(fake_lease_redis: Any) -> None:
    async def go() -> None:
        # A distinct workspace is independently lockable at the same time.
        async with lease.workspace_lease("ws-a", lease_ms=60_000), lease.workspace_lease("ws-b", lease_ms=60_000):
            pass
        # The lease key is dropped on normal release.
        assert await fake_lease_redis.get(lease._wslock_key("ws-a")) is None

    asyncio.run(go())
