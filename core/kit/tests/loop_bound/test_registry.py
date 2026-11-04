"""Loop-keyed registry machinery: epoch scoping and the close of a retired epoch's registries."""

import asyncio

import pytest

from tai42_kit.clients.base import advance_client_epoch, current_client_epoch
from tai42_kit.loop_bound import LoopRegistryMap, ResourceRegistry, close_retired_registries

from ._registries import closer, registry_with


def _held_epochs(rmap: LoopRegistryMap[ResourceRegistry]) -> set[int]:
    return set(rmap._registries.get(asyncio.get_running_loop(), {}))


async def test_closing_a_retired_epoch_closes_and_drops_only_that_epoch_of_every_map():
    log: list[str] = []
    first: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    second: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    await registry_with(first, closer(log, "first-retired"))
    await registry_with(second, closer(log, "second-retired"))
    retired = advance_client_epoch()
    current_first = await registry_with(first, closer(log, "first-current"))
    current_second = await registry_with(second, closer(log, "second-current"))

    await close_retired_registries(retired)

    assert sorted(log) == ["first-retired", "second-retired"]
    assert _held_epochs(first) == _held_epochs(second) == {current_client_epoch()}
    assert current_first.has_live_resources is True
    assert current_second.has_live_resources is True


async def test_closing_the_current_epoch_is_refused():
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    registry = await registry_with(rmap, closer([], "current"))
    with pytest.raises(ValueError, match="current epoch"):
        await close_retired_registries(current_client_epoch())
    assert registry.has_live_resources is True


async def test_a_failing_retired_close_is_raised_after_every_other_map_closed():
    log: list[str] = []
    failing: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    healthy: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    await registry_with(failing, closer(log, "failing", fails=True))
    await registry_with(healthy, closer(log, "healthy"))
    retired = advance_client_epoch()

    with pytest.raises(ExceptionGroup, match="errors closing the retired epoch's loop-bound registries") as excinfo:
        await close_retired_registries(retired)

    assert sorted(log) == ["failing", "healthy"]
    assert len(excinfo.value.exceptions) == 1
    assert _held_epochs(failing) == _held_epochs(healthy) == set()


async def test_a_retired_epoch_with_no_registry_on_the_loop_closes_nothing():
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    retired = advance_client_epoch()
    current = await registry_with(rmap, closer([], "current"))
    await close_retired_registries(retired)
    assert current.has_live_resources is True


async def test_current_rebuilds_per_epoch():
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry)
    first = rmap.current()
    assert rmap.current() is first  # same epoch -> same registry
    advance_client_epoch()
    second = rmap.current()
    assert second is not first  # new epoch -> fresh registry
