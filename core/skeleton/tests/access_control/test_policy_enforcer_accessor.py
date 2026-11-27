"""``policy_enforcer``: one ``PolicyEnforcer`` per settings object, rebuilt on a settings change.

The decision-level proofs (a hundred nested decisions build one enforcer; a writer's bump is
seen by the next decision) live beside the seam's fixtures in ``tests/authz/test_execution_seam.py``.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from tai42_contract.access_control.models import AccessPolicy
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.access_control.policy import PolicyEnforcer, policy_enforcer
from tai42_skeleton.access_control.settings import AccessControlSettings


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


def test_one_enforcer_and_its_policy_cache_serve_every_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _settings()
    fetched: list[str] = []
    first_fetching = threading.Event()
    second_asked = threading.Event()

    async def held_policy(self: PolicyEnforcer, user_id: str) -> AccessPolicy:
        fetched.append(user_id)
        first_fetching.set()
        # Held off the loop thread until the other loop has asked, so both loops ask while a fetch is in flight.
        assert await asyncio.to_thread(second_asked.wait, 10)
        return AccessPolicy()

    monkeypatch.setattr(PolicyEnforcer, "_raw_fetch_policy", held_policy)

    async def ask(*, signal: threading.Event | None) -> PolicyEnforcer:
        enforcer = policy_enforcer(settings)
        if signal is not None:
            signal.set()
        assert await enforcer.get_policy_at("u1", 0) == AccessPolicy()
        return enforcer

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(asyncio.run, ask(signal=None))
        assert first_fetching.wait(10)
        second = pool.submit(asyncio.run, ask(signal=second_asked))

        second_enforcer = second.result(timeout=10)
        first_enforcer = first.result(timeout=10)
    assert first_enforcer is second_enforcer
    assert fetched == ["u1", "u1"]

    assert asyncio.run(ask(signal=None)) is first_enforcer
    assert fetched == ["u1", "u1"]

    reset_all_settings()
    assert asyncio.run(ask(signal=None)) is not first_enforcer
    assert fetched == ["u1", "u1", "u1"]
