"""``AppTools.surface_generation``: every change of the listed tool set moves it.

Proven through a neutral synthetic consumer — a cache of the sorted tool listing keyed on the
generation — that must miss after every door that adds or removes a tool, and hit otherwise.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding.surface import add_surface_tool, tool_surface_generation

from ..app.lifecycle._doubles import _cfg, _Mixin


class _ListingCache:
    """Caches a sorted tool listing, keyed on the surface generation it was read under."""

    def __init__(self, list_names: Callable[[], Awaitable[list[str]]], generation: Callable[[], int]) -> None:
        self._list_names = list_names
        self._generation = generation
        self._held: tuple[int, list[str]] | None = None
        self.loads = 0

    async def listing(self) -> list[str]:
        generation = self._generation()
        if self._held is not None and self._held[0] == generation:
            return self._held[1]
        names = sorted(await self._list_names())
        self.loads += 1
        self._held = (generation, names)
        return names


def _app_cache() -> _ListingCache:
    async def names() -> list[str]:
        return list(await app.tools.get_tools())

    return _ListingCache(names, app.tools.surface_generation)


async def first_probe() -> str:
    """A probe tool."""
    return "first"


async def second_probe() -> str:
    """A second probe tool."""
    return "second"


def test_the_listing_hits_without_a_mutation_and_misses_after_each_registration_door() -> None:
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            cache = _app_cache()
            start = app.tools.surface_generation()
            await cache.listing()
            await cache.listing()
            assert cache.loads == 1

            app.tools.tool(force=True)(first_probe)
            assert "first_probe" in await cache.listing()
            assert cache.loads == 2

            await app.preset_manager.register("first_preset", "first_probe", {}, [], "a preset")
            assert "first_preset" in await cache.listing()
            assert cache.loads == 3

            await app.preset_manager.remove("first_preset")
            assert "first_preset" not in await cache.listing()
            assert cache.loads == 4

            app.tools.remove_tool("first_probe")
            assert "first_probe" not in await cache.listing()
            assert cache.loads == 5

            await cache.listing()
            assert cache.loads == 5
            assert app.tools.surface_generation() > start

    asyncio.run(run())


def test_resetting_the_component_surface_moves_the_generation() -> None:
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            app.tools.tool(force=True)(first_probe)
            cache = _app_cache()
            assert "first_probe" in await cache.listing()

            app._reset_component_surface()

            assert "first_probe" not in await cache.listing()
            assert cache.loads == 2

    asyncio.run(run())


def test_an_mcp_reload_rebinding_a_server_moves_the_generation() -> None:
    mixin = _Mixin()
    mixin._manifest = Manifest.model_validate({"mcp": [_cfg("svc").model_dump()]})
    mixin._probe_mcp = AsyncMock(return_value=[])

    @mixin._fast_mcp.tool(name="svc_t")
    def svc_t() -> str:
        return "t"

    mixin._mcp_bound_tools["svc"] = {"svc_t"}

    async def names() -> list[str]:
        return [tool.name for tool in await mixin._fast_mcp.list_tools()]

    cache = _ListingCache(names, tool_surface_generation)
    assert asyncio.run(cache.listing()) == ["svc_t"]

    assert mixin._reload_mcp("svc")["status"] == "ok"

    assert asyncio.run(cache.listing()) == []
    assert cache.loads == 2


def test_a_new_serving_core_moves_the_generation() -> None:
    before = tool_surface_generation()
    mixin = _Mixin()
    mixin._build_serving_core()
    assert tool_surface_generation() > before


def test_a_failed_profile_apply_build_moves_the_generation() -> None:
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            built_under: list[int] = []

            def rebuild() -> None:
                # The building core gets a tool and a reader caches its listing under the
                # generation it reads now; then the build fails and the core is discarded.
                app._building = app._build_serving_core()
                add_surface_tool(app._fast_mcp, _tool_object())
                built_under.append(app.tools.surface_generation())
                app._building = None
                raise RuntimeError("deliberate build failure")

            async def build_serving_app(epoch: Epoch) -> Any:
                raise AssertionError("never reached")

            with pytest.raises(RuntimeError, match="deliberate build failure"):
                await build_and_swap_epoch({}, rebuild=rebuild, build_serving_app=build_serving_app)

            assert app.tools.surface_generation() > built_under[0]
            assert "second_probe" not in await app.tools.get_tools()

    asyncio.run(run())


def _tool_object() -> Any:
    from fastmcp.tools import Tool

    return Tool.from_function(second_probe)


def test_the_generation_never_decreases() -> None:
    seen = [tool_surface_generation()]

    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            seen.append(app.tools.surface_generation())
            app.tools.tool(force=True)(second_probe)
            seen.append(app.tools.surface_generation())
            app.tools.remove_tool("second_probe")
            seen.append(app.tools.surface_generation())

    asyncio.run(run())
    seen.append(tool_surface_generation())
    assert seen == sorted(seen)


# -- the chokepoint is structural ---------------------------------------------------------------

_SKELETON_SRC = Path(__file__).resolve().parents[2] / "src" / "tai42_skeleton"
_SURFACE_MODULE = _SKELETON_SRC / "tools" / "binding" / "surface.py"
# A sub-MCP mount is its own server, not the listed surface.
_OTHER_SERVERS = {_SKELETON_SRC / "app" / "sub_mcp_app.py"}
# Receivers whose ``enable``/``disable`` names no listed component: the monitoring writer's
# ``disable()`` suspends record writing for a block.
_NOT_COMPONENTS = {"get_monitoring().writer"}


def _calls(attribute: str) -> list[str]:
    hits: list[str] = []
    for path in sorted(_SKELETON_SRC.rglob("*.py")):
        if path == _SURFACE_MODULE or path in _OTHER_SERVERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == attribute:
                receiver = ast.unparse(node.func.value)
                if receiver in _NOT_COMPONENTS:
                    continue
                hits.append(f"{path.relative_to(_SKELETON_SRC)}:{node.lineno} {receiver}.{attribute}")
    return hits


def test_no_tool_is_added_to_or_removed_from_the_main_server_outside_the_surface_module() -> None:
    added = _calls("add_tool")
    removed = [hit for hit in _calls("remove_tool") if "local_provider" in hit or "provider." in hit]
    assert added == []
    assert removed == []


def test_nothing_enables_disables_or_transforms_the_listed_components() -> None:
    # The generation describes what one server's lookups answer only while no component is
    # enabled, disabled or transformed outside the add/remove chokepoint.
    hits = [*_calls("enable"), *_calls("disable"), *_calls("add_transform"), *_calls("enable_components")]
    assert hits == []
