"""The run-target index: a tool name is looked up once per serving core and tool-surface generation.

``FastMCP.get_tool`` is wrapped with a counter, so every case counts which lookups asked the
server and which were answered from the serving core's index.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastmcp import FastMCP
from fastmcp.tools import Tool

from tai42_skeleton.app import epoch as epoch_mod
from tai42_skeleton.app import retired_generations as retired_mod
from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch
from tai42_skeleton.app.instance import app
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.tools.binding.errors import UnknownToolError
from tai42_skeleton.tools.binding.surface import add_surface_tool, bump_tool_surface_generation


async def first_probe() -> str:
    """A probe tool."""
    return "first"


async def second_probe() -> str:
    """Another probe tool."""
    return "second"


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Every name ``FastMCP.get_tool`` was asked for."""
    names: list[str] = []
    real = FastMCP.get_tool

    async def counting(self: FastMCP, name: str, *args: Any, **kwargs: Any) -> Any:
        names.append(name)
        return await real(self, name, *args, **kwargs)

    monkeypatch.setattr(FastMCP, "get_tool", counting)
    return names


def _run(body: Any) -> None:
    async def run() -> None:
        async with app.app_context(Manifest.model_validate({})):
            await body()

    asyncio.run(run())


def test_repeated_lookups_ask_the_server_once(asked: list[str]) -> None:
    async def body() -> None:
        app.tools.tool(force=True)(first_probe)
        tools = [await app._tool_binding._resolve_run_target("first_probe") for _ in range(5)]
        assert all(tool is tools[0] for tool in tools)
        assert asked == ["first_probe"]

    _run(body)


def test_the_facet_and_the_run_target_share_the_index(asked: list[str]) -> None:
    async def body() -> None:
        app.tools.tool(force=True)(first_probe)
        via_facet = await app.tools.get_tool("first_probe")
        via_run = await app._tool_binding._resolve_run_target("first_probe")
        assert via_facet is via_run
        assert asked == ["first_probe"]

    _run(body)


def test_every_surface_door_makes_the_next_lookup_ask_again(asked: list[str]) -> None:
    async def asks(name: str) -> int:
        """How many times one lookup of ``name`` asked the server (a miss raises out)."""
        before = len(asked)
        await app.tools.get_tool(name)
        return len(asked) - before

    async def body() -> None:
        app.tools.tool(force=True)(first_probe)
        assert (await asks("first_probe"), await asks("first_probe")) == (1, 0)

        app.tools.tool(force=True)(second_probe)  # a registration
        assert (await asks("first_probe"), await asks("first_probe")) == (1, 0)

        await app.preset_manager.register("probe_preset", "first_probe", {}, [], "a preset")
        assert (await asks("first_probe"), await asks("probe_preset"), await asks("probe_preset")) == (1, 1, 0)
        await app.preset_manager.remove("probe_preset")
        with pytest.raises(UnknownToolError):
            await app.tools.get_tool("probe_preset")
        assert (await asks("first_probe"), await asks("first_probe")) == (1, 0)

        app.tools.remove_tool("first_probe")  # a removal
        with pytest.raises(UnknownToolError):
            await app.tools.get_tool("first_probe")

        app.tools.tool(force=True)(first_probe)
        assert (await asks("first_probe"), await asks("first_probe")) == (1, 0)
        app._reset_component_surface()  # the component surface reset
        with pytest.raises(UnknownToolError):
            await app.tools.get_tool("first_probe")

    _run(body)


def test_an_unknown_name_is_never_stored(asked: list[str]) -> None:
    async def body() -> None:
        for _ in range(2):
            with pytest.raises(UnknownToolError):
                await app.tools.get_tool("first_probe")
        app.tools.tool(force=True)(first_probe)
        assert (await app.tools.get_tool("first_probe")).name == "first_probe"
        assert asked == ["first_probe", "first_probe", "first_probe"]

    _run(body)


def test_a_surface_change_during_a_lookup_leaves_its_answer_unserved(
    asked: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body() -> None:
        app.tools.tool(force=True)(first_probe)
        real = FastMCP.get_tool

        async def bumping(self: FastMCP, name: str, *args: Any, **kwargs: Any) -> Any:
            tool = await real(self, name, *args, **kwargs)  # the counting wrapper records the ask
            bump_tool_surface_generation()  # the surface moved while the server answered
            return tool

        monkeypatch.setattr(FastMCP, "get_tool", bumping)
        await app.tools.get_tool("first_probe")
        await app.tools.get_tool("first_probe")
        assert asked == ["first_probe", "first_probe"]

    _run(body)


def test_a_new_cores_index_is_born_empty() -> None:
    async def body() -> None:
        core = app._build_serving_core()
        assert core._tool_index.lookup("first_probe", 0) is None

    _run(body)


def _tool(fn: Any) -> Tool:
    return Tool.from_function(fn, name="probe")


async def _serving_handle(scope: Any, receive: Any, send: Any) -> None:
    """The generation's ASGI handle; the swap records it (weakly) as the serving surface."""


@pytest.fixture
def _epoch_state() -> Iterator[None]:
    saved = {name: getattr(epoch_mod, name) for name in ("_current", "_serving_slot")}
    try:
        yield
    finally:
        for name, value in saved.items():
            setattr(epoch_mod, name, value)
        epoch_mod._building_epoch = None
        retired_mod.detach_retired_generations()


@pytest.mark.usefixtures("_epoch_state")
def test_two_coexisting_cores_each_answer_with_their_own_tool(asked: list[str]) -> None:
    async def body() -> None:
        add_surface_tool(app._fast_mcp, _tool(first_probe))
        live_core = app._serving_core
        live_tool = await app.tools.get_tool("probe")
        seen: dict[str, Any] = {}

        def rebuild() -> None:
            app._building = app._build_serving_core()
            add_surface_tool(app._fast_mcp, _tool(second_probe))  # the building core's own "probe"

        async def build_serving_app(epoch: Epoch) -> Any:
            core = app._building
            try:
                # During the build ``get_tool`` reads the building core; the live core still
                # answers with its own tool when asked directly.
                seen["building"] = await app.tools.get_tool("probe")
                seen["live"] = await live_core._fast_mcp.get_tool("probe")
                await asyncio.sleep(0)
                epoch.core = core
            finally:
                app._building = None
            return _serving_handle

        async def no_loops() -> None:
            return None

        await build_and_swap_epoch(
            {}, rebuild=rebuild, build_serving_app=build_serving_app, establish_background_loops=no_loops
        )

        assert seen["building"] is not seen["live"]
        assert seen["live"] is live_tool
        after = await app.tools.get_tool("probe")
        assert after is seen["building"]
        assert after is not live_tool

    _run(body)


@pytest.mark.usefixtures("_epoch_state")
def test_a_discarded_build_never_answers_for_the_live_core(asked: list[str]) -> None:
    async def body() -> None:
        add_surface_tool(app._fast_mcp, _tool(first_probe))
        live_tool = await app.tools.get_tool("probe")

        def rebuild() -> None:
            app._building = app._build_serving_core()
            add_surface_tool(app._fast_mcp, _tool(second_probe))

        async def build_serving_app(epoch: Epoch) -> Any:
            try:
                await app.tools.get_tool("probe")  # indexed on the building core
                raise RuntimeError("deliberate build failure")
            finally:
                app._building = None

        with pytest.raises(RuntimeError, match="deliberate build failure"):
            await build_and_swap_epoch({}, rebuild=rebuild, build_serving_app=build_serving_app)

        assert await app.tools.get_tool("probe") is live_tool

    _run(body)
