"""Shared loop-keyed registry infra: the fail-loud vs drop-on-closed-loop reset.

``LoopRegistryMap.reset`` is exercised end-to-end through the checkpoint/store
settings-reset tests; here it is driven directly with fake loops to pin the
closed-loop branch (whose resources cannot be closed from a sync reset).
"""

import logging
from asyncio import AbstractEventLoop
from typing import cast

import pytest

pytest.importorskip("langgraph")

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


def _registry_with_resource() -> ResourceRegistry:
    reg = ResourceRegistry()
    reg._resources = {"k": (object(), None)}
    return reg


def test_reset_raises_on_running_loop_with_live_resources():
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    loop = _FakeLoop(closed=False)  # strong ref so the weak-keyed entry survives
    rmap._registries[_as_loop(loop)] = _registry_with_resource()
    with pytest.raises(RuntimeError, match="close_all"):
        rmap.reset()
    # The map is left intact so the caller can close_all() and retry.
    assert len(rmap._registries) == 1


def test_reset_drops_closed_loop_registry_and_logs(caplog):
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    loop = _FakeLoop(closed=True)  # strong ref so the weak-keyed entry survives
    rmap._registries[_as_loop(loop)] = _registry_with_resource()
    with caplog.at_level(logging.WARNING, logger="tai42_kit.llm._resource_registry"):
        rmap.reset()
    # A closed loop's resources can't be closed here, so the registry is dropped
    # (never silently — the drop of a still-populated registry is logged).
    assert rmap._registries == {}
    assert any("closed event loop" in r.message for r in caplog.records)


def test_reset_clears_empty_registries_without_logging(caplog):
    rmap: LoopRegistryMap[ResourceRegistry] = LoopRegistryMap(ResourceRegistry, "Reg")
    loop = _FakeLoop(closed=False)  # strong ref so the weak-keyed entry survives
    rmap._registries[_as_loop(loop)] = ResourceRegistry()  # no live resources
    with caplog.at_level(logging.WARNING, logger="tai42_kit.llm._resource_registry"):
        rmap.reset()
    assert rmap._registries == {}
    assert caplog.records == []
