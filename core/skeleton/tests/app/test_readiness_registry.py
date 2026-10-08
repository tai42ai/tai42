"""The declared readiness registry, proven with a made-up subsystem."""

from __future__ import annotations

import pytest
from tai42_contract.access_control.identity import ReadinessTarget
from tai42_kit.clients import RedisConnectionSettings
from tai42_kit.clients.impl.redis import RedisClient

from tai42_skeleton.app.readiness import ReadinessRegistry


def test_a_registered_subsystem_contributes_its_targets_in_registration_order() -> None:
    registry = ReadinessRegistry()
    first = ReadinessTarget("widget_cache", RedisClient, RedisConnectionSettings(redis_url="redis://widgets"))
    second = ReadinessTarget("gadget_queue", RedisClient, RedisConnectionSettings(redis_url="redis://gadgets"))
    registry.register("widgets", lambda: [first])
    registry.register("gadgets", lambda: (second,))
    assert registry.wired_targets() == [first, second]


def test_a_contributor_is_read_on_every_call() -> None:
    registry = ReadinessRegistry()
    wired: list[ReadinessTarget] = []
    registry.register("widgets", lambda: list(wired))
    assert registry.wired_targets() == []
    target = ReadinessTarget("widget_cache", RedisClient, RedisConnectionSettings(redis_url="redis://widgets"))
    wired.append(target)
    assert registry.wired_targets() == [target]


def test_a_duplicate_subsystem_name_is_refused() -> None:
    registry = ReadinessRegistry()
    registry.register("widgets", list)
    with pytest.raises(ValueError, match="widgets"):
        registry.register("widgets", list)


def test_two_registries_hold_their_own_contributors() -> None:
    one, other = ReadinessRegistry(), ReadinessRegistry()
    one.register("widgets", list)
    other.register("widgets", list)
    assert one.wired_targets() == other.wired_targets() == []
