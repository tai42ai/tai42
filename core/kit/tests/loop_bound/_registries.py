"""Probe resources for the loop-bound registry tests: a closer that notes its run, and a registry holding one."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from tai42_kit.loop_bound import LoopRegistryMap, ResourceRegistry


def closer(log: list[str], name: str, *, fails: bool = False) -> Callable[[], Awaitable[None]]:
    """A resource closer that appends ``name`` to ``log`` when it runs, and then raises when ``fails``."""

    async def _close() -> None:
        log.append(name)
        if fails:
            raise RuntimeError(f"{name} close failed")

    return _close


async def registry_with(
    rmap: LoopRegistryMap[ResourceRegistry], close: Callable[[], Awaitable[None]], *, key: str = "k"
) -> ResourceRegistry:
    """The running loop's current-epoch registry of ``rmap``, holding one resource under ``key`` closed by ``close``."""
    registry = rmap.current()

    async def _factory() -> tuple[Any, Callable[[], Awaitable[None]]]:
        return object(), close

    await registry._get_or_init_resource(key, _factory)
    return registry
