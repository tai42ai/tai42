"""One call releases every loop-bound registry on the running loop, trying every one before it raises."""

from __future__ import annotations

import pytest

from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.loop_bound import LoopRegistryMap, ResourceRegistry
from tai42_kit.loop_bound.registry import _release_registries

from ._registries import closer, registry_with


async def test_a_map_built_anywhere_is_released_by_the_one_call():
    log: list[str] = []
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    registry = await registry_with(rmap, closer(log, "probe"))
    await _release_registries()
    assert log == ["probe"]
    assert registry.has_live_resources is False
    # The next use on the loop builds a fresh registry.
    assert rmap.current() is not registry


async def test_a_failure_is_raised_after_every_other_registry_closed():
    log: list[str] = []
    failing: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    healthy: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    await registry_with(failing, closer(log, "failing", fails=True))
    await registry_with(healthy, closer(log, "healthy"))
    with pytest.raises(ExceptionGroup, match="errors releasing the loop's loop-bound registries") as excinfo:
        await _release_registries()
    assert sorted(log) == ["failing", "healthy"]
    assert len(excinfo.value.exceptions) == 1


async def test_one_call_releases_every_epoch_registry_of_the_running_loop():
    log: list[str] = []
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    retired = await registry_with(rmap, closer(log, "retired"), key="old")
    advance_client_epoch()
    current = await registry_with(rmap, closer(log, "current"), key="new")
    await _release_registries()
    assert sorted(log) == ["current", "retired"]
    assert current.has_live_resources is False
    assert retired.has_live_resources is False


async def test_no_registry_on_the_loop_is_a_noop():
    LoopRegistryMap(ResourceRegistry)
    await _release_registries()
