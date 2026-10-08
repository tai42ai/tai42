"""``policy_enforcer``: one ``PolicyEnforcer`` per running event loop, rebuilt on a settings change.

The decision-level proofs (a hundred nested decisions build one enforcer; a writer's bump is
seen by the next decision) live beside the seam's fixtures in ``tests/authz/test_execution_seam.py``.
"""

from __future__ import annotations

import asyncio
import gc
import threading

import pytest
from tai42_contract.access_control.models import AccessPolicy
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.access_control import policy as policy_module
from tai42_skeleton.access_control.policy import PolicyEnforcer, policy_enforcer
from tai42_skeleton.access_control.settings import AccessControlSettings

pytestmark = pytest.mark.filterwarnings("ignore::async_lru.AlruCacheLoopResetWarning")


def _settings() -> AccessControlSettings:
    return AccessControlSettings(enable=True)


def test_one_loop_reuses_one_enforcer_across_awaits() -> None:
    settings = _settings()

    async def run() -> tuple[PolicyEnforcer, PolicyEnforcer]:
        first = policy_enforcer(settings)
        await asyncio.sleep(0)
        second = policy_enforcer(settings)
        return first, second

    first, second = asyncio.run(run())
    assert first is second
    assert first.settings is settings


def test_two_loops_get_two_enforcers() -> None:
    settings = _settings()
    built: list[PolicyEnforcer] = []

    async def run() -> None:
        built.append(policy_enforcer(settings))

    threads = [threading.Thread(target=asyncio.run, args=(run(),)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(built) == 2
    assert built[0] is not built[1]


def test_a_settings_reset_rebuilds_the_enforcer() -> None:
    settings = _settings()

    async def run() -> None:
        before = policy_enforcer(settings)
        reset_all_settings()
        assert policy_enforcer(settings) is not before

    asyncio.run(run())


def test_a_different_settings_object_rebuilds_the_enforcer() -> None:
    async def run() -> None:
        first = policy_enforcer(_settings())
        other_settings = _settings()
        second = policy_enforcer(other_settings)
        assert second is not first
        assert second.settings is other_settings
        assert policy_enforcer(other_settings) is second

    asyncio.run(run())


def test_outside_a_running_loop_the_accessor_raises() -> None:
    with pytest.raises(RuntimeError, match="no running event loop"):
        policy_enforcer(_settings())


def test_a_failing_build_stores_nothing_and_the_next_call_builds_again(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    attempts = 0
    real_init = PolicyEnforcer.__init__

    def flaky_init(self: PolicyEnforcer, given: AccessControlSettings) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("enforcer build failed")
        real_init(self, given)

    monkeypatch.setattr(PolicyEnforcer, "__init__", flaky_init)

    async def run() -> None:
        with pytest.raises(RuntimeError, match="enforcer build failed"):
            policy_enforcer(settings)
        built = policy_enforcer(settings)
        assert policy_enforcer(settings) is built

    asyncio.run(run())
    assert attempts == 2


def test_a_closed_loops_enforcer_is_not_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()

    async def empty_policy(self: PolicyEnforcer, user_id: str) -> AccessPolicy:
        return AccessPolicy()

    monkeypatch.setattr(PolicyEnforcer, "_raw_fetch_policy", empty_policy)
    held_while_running: list[int] = []

    async def run() -> None:
        # A used policy cache keeps its loop referenced, so only an explicit drop frees it.
        await policy_enforcer(settings).get_policy_at("u1", 0)
        gc.collect()
        held_while_running.append(len(policy_module._enforcers))

    asyncio.run(run())
    asyncio.run(run())
    assert held_while_running == [1, 1]
