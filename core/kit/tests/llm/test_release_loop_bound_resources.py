"""One call releases every loop-bound kit registry on the running loop, trying every one before it raises."""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.llm import release_loop_bound_resources
from tai42_kit.llm._resource_registry import LoopRegistryMap, ResourceRegistry


def _closer(log: list[str], name: str, *, fails: bool = False):
    async def _close() -> None:
        log.append(name)
        if fails:
            raise RuntimeError(f"{name} close failed")

    return _close


async def _registry_with(rmap: LoopRegistryMap[ResourceRegistry], key: str, closer) -> ResourceRegistry:
    registry = rmap.current()

    async def _factory():
        return object(), closer

    await registry._get_or_init_resource(key, _factory)
    return registry


async def test_a_map_built_anywhere_is_released_by_the_one_call():
    log: list[str] = []
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Probe")
    registry = await _registry_with(rmap, "k", _closer(log, "probe"))
    await release_loop_bound_resources()
    assert log == ["probe"]
    assert registry.has_live_resources is False
    # The next use on the loop builds a fresh registry.
    assert rmap.current() is not registry


async def test_a_failure_is_raised_after_every_other_registry_closed():
    log: list[str] = []
    failing: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Failing")
    healthy: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Healthy")
    await _registry_with(failing, "k", _closer(log, "failing", fails=True))
    await _registry_with(healthy, "k", _closer(log, "healthy"))
    with pytest.raises(ExceptionGroup, match="errors releasing loop-bound kit resources") as excinfo:
        await release_loop_bound_resources()
    assert sorted(log) == ["failing", "healthy"]
    assert len(excinfo.value.exceptions) == 1


async def test_a_retired_epoch_registry_is_released_only_with_all_epochs():
    log: list[str] = []
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Epochs")
    retired = await _registry_with(rmap, "old", _closer(log, "retired"))
    advance_client_epoch()
    current = await _registry_with(rmap, "new", _closer(log, "current"))
    await release_loop_bound_resources()
    assert log == ["current"]
    assert retired.has_live_resources is True
    await release_loop_bound_resources(all_epochs=True)
    assert log == ["current", "retired"]
    assert current.has_live_resources is False
    assert retired.has_live_resources is False


async def test_no_registry_on_the_loop_is_a_noop():
    LoopRegistryMap(ResourceRegistry, "Unused")
    await release_loop_bound_resources(all_epochs=True)


async def test_the_checkpoint_and_store_registries_are_released():
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry

    registry = checkpoint_registry()
    await registry.get_checkpointer("memory", None)
    assert registry.has_live_resources is True
    await release_loop_bound_resources()
    assert registry.has_live_resources is False
