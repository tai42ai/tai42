"""Shared loop-keyed registry infra: the fail-loud vs drop-on-closed-loop reset,
epoch scoping, and the retired-epoch drain.

``LoopRegistryMap.reset`` is exercised end-to-end through the checkpoint/store
settings-reset tests; here it is driven directly with fake loops to pin the
closed-loop branch (whose resources cannot be closed from a sync reset) and the
epoch-scoped refuse-vs-drain policy. The async drain is driven on a real loop.
"""

import logging
from asyncio import AbstractEventLoop
from typing import cast

import pytest

pytest.importorskip("langgraph")

from tai42_kit.clients.base import advance_client_epoch
from tai42_kit.llm._resource_registry import LoopRegistryMap, ResourceRegistry


class _FakeLoop:
    """A weakly-referenceable stand-in for an event loop with a fixed closed state.

    ``reset`` only reads ``is_closed()`` off the map's keys, so a minimal fake
    with a fixed closed state stands in for a real ``AbstractEventLoop``.
    """

    def __init__(self, *, closed: bool) -> None:
        self._closed = closed

    def is_closed(self) -> bool:
        return self._closed


def _as_loop(fake: _FakeLoop) -> AbstractEventLoop:
    return cast(AbstractEventLoop, fake)


def _registry_with_resource(closer=None) -> ResourceRegistry:
    reg = ResourceRegistry()
    reg._resources = {"k": (object(), closer)}
    return reg


def test_reset_raises_on_current_epoch_running_loop_with_live_resources():
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    loop = _FakeLoop(closed=False)  # strong ref so the weak-keyed entry survives
    rmap._registries[_as_loop(loop)] = {0: _registry_with_resource()}
    with pytest.raises(RuntimeError, match="close_all"):
        rmap.reset()
    # The map is left intact so the caller can close_all() and retry.
    assert len(rmap._registries) == 1


def test_reset_drops_closed_loop_registry_and_logs(caplog):
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    loop = _FakeLoop(closed=True)  # strong ref so the weak-keyed entry survives
    rmap._registries[_as_loop(loop)] = {0: _registry_with_resource()}
    with caplog.at_level(logging.WARNING, logger="tai42_kit.llm._resource_registry"):
        rmap.reset()
    # A closed loop's resources can't be closed here, so the registry is dropped
    # (never silently — the drop of a still-populated registry is logged).
    assert rmap._registries == {}
    assert any("closed event loop" in r.message for r in caplog.records)


def test_reset_clears_empty_registries_without_logging(caplog):
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    loop = _FakeLoop(closed=False)  # strong ref so the weak-keyed entry survives
    rmap._registries[_as_loop(loop)] = {0: ResourceRegistry()}  # no live resources
    with caplog.at_level(logging.WARNING, logger="tai42_kit.llm._resource_registry"):
        rmap.reset()
    assert rmap._registries == {}
    assert caplog.records == []


def test_reset_excludes_retired_epoch_live_resources():
    # A retired epoch's registry on a still-running loop is NOT a reset error: it
    # drains as its resources release (drain_epoch owns it). reset() must neither
    # raise nor drop it.
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    loop = _FakeLoop(closed=False)
    rmap._registries[_as_loop(loop)] = {0: _registry_with_resource()}
    advance_client_epoch()  # retire epoch 0; current epoch is now 1
    rmap.reset()  # does not raise
    # The retired-epoch registry is left in place for drain_epoch.
    assert rmap._registries[_as_loop(loop)] == {0: rmap._registries[_as_loop(loop)][0]}


def test_reset_drops_current_epoch_keeps_retired():
    # With a retired (0) and a current (1) registry on one loop, reset drops only
    # the current-epoch one; the retired one survives for the drain.
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    loop = _FakeLoop(closed=False)
    retired = ResourceRegistry()  # no live resources -> current-epoch drop is silent
    advance_client_epoch()  # current epoch is now 1
    rmap._registries[_as_loop(loop)] = {0: _registry_with_resource(), 1: retired}
    rmap.reset()
    assert set(rmap._registries[_as_loop(loop)]) == {0}


async def test_drain_epoch_closes_retired_registry_on_the_owning_loop():
    closed = []

    async def _closer() -> None:
        closed.append(True)

    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    reg = rmap.current()  # keyed under the running loop at epoch 0
    reg._resources["k"] = (object(), _closer)
    advance_client_epoch()  # retire epoch 0
    await rmap.drain_epoch(0, 0.05)
    # close_all ran the resource's closer and the retired entry is gone.
    assert closed == [True]
    assert len(rmap._registries) == 0


async def test_drain_epoch_refuses_current_epoch():
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    rmap.current()
    with pytest.raises(ValueError, match="current epoch"):
        await rmap.drain_epoch(0, 0.0)


async def test_drain_epoch_missing_is_noop():
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    advance_client_epoch()  # current epoch 1; nothing registered at epoch 0
    await rmap.drain_epoch(0, 0.0)  # no registry for this loop/epoch -> returns


async def test_current_rebuilds_per_epoch():
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    first = rmap.current()
    assert rmap.current() is first  # same epoch -> same registry
    advance_client_epoch()
    second = rmap.current()
    assert second is not first  # new epoch -> fresh registry
