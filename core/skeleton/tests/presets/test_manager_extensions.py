"""Preset extension branches: a wrapper branch keeps the preset description, two
independent combos survive versioning, remove tears down base plus branches, and a
body description projects to the tool and survives reload."""

from __future__ import annotations

import asyncio

import pytest

from tai42_skeleton.app.instance import app
from tai42_skeleton.tools.binding import UnknownToolError

from ._manager_fixtures import FakeVersioningPg, _create_versioned, _live_tool_names, _manifest


def test_wrapper_branch_of_preset_keeps_preset_description(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            # ``exta`` is a ``functools.wraps`` wrapper, so its branch inherits the
            # base callable's docstring. The adoption guard must compare against the
            # BRANCH BASE callable's docstring (not the ``Tool`` object's class
            # docstring), so the wrapper is recognized as authoring no new
            # description and the preset's own description survives onto the branch.
            await _create_versioned("shouty", "echo", {}, [["exta"]], description="Preset desc")

            base = await app.tools.get_tool("shouty")
            branch = await app.tools.get_tool("shouty_exta")
            assert base.description == "Preset desc"
            assert branch.description == "Preset desc"

    asyncio.run(run())


def test_extensions_two_combos_survive_versioning(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("shouty", "echo", {}, [["exta"], ["extb"]])

            # Bare name runnable + BOTH independent branches + NO stacked branch.
            tools = set(await app.tools.get_tools())
            assert {"shouty", "shouty_exta", "shouty_extb"} <= tools
            assert "shouty_exta_extb" not in tools
            assert await app.tools.run_tool("shouty", {"text": "hi"}) == "hi"
            assert await app.tools.run_tool("shouty_exta", {"text": "hi"}) == "hi|a"
            assert await app.tools.run_tool("shouty_extb", {"text": "hi"}) == "hi|b"

            # Save a new version WITHOUT passing extensions, then reload: the new
            # active body carried base_tool + BOTH combos forward.
            await store.save_version("shouty", fixed_kwargs={})
            await mgr.reload("shouty")
            tools = set(await app.tools.get_tools())
            assert {"shouty", "shouty_exta", "shouty_extb"} <= tools
            assert "shouty_exta_extb" not in tools
            assert await app.tools.run_tool("shouty_exta", {"text": "yo"}) == "yo|a"

            # Each branch is bound EXACTLY once (the reload's teardown ran before
            # re-register — no leaked pre-reload duplicate), and the base's
            # _extend_tools holds exactly one entry per branch.
            names = _live_tool_names()
            assert names.count("shouty_exta") == 1
            assert names.count("shouty_extb") == 1
            branches = {b for b, base in app._tool_registry._extend_tools.items() if base == "shouty" and b != "shouty"}
            assert branches == {"shouty_exta", "shouty_extb"}

    asyncio.run(run())


def test_remove_tears_down_base_and_branches(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("shouty", "echo", {}, [["exta"], ["extb"]])
            assert {"shouty", "shouty_exta", "shouty_extb"} <= set(await app.tools.get_tools())

            await mgr.remove("shouty")

            tools = set(await app.tools.get_tools())
            assert not ({"shouty", "shouty_exta", "shouty_extb"} & tools)
            assert not mgr.is_registered("shouty")
            for name in ("shouty", "shouty_exta", "shouty_extb"):
                with pytest.raises(UnknownToolError):
                    await app.tools.run_tool(name, {"text": "hi"})

    asyncio.run(run())


def test_body_description_projects_to_tool_and_survives_reload(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [], description="Weather v1")
            assert (await app.tools.get_tool("wv")).description == "Weather v1"

            # A simulated reload wipes the runtime tool; rehydrate re-projects the
            # description from the persisted body.
            await mgr.remove("wv")
            await mgr.rehydrate()
            assert (await app.tools.get_tool("wv")).description == "Weather v1"

    asyncio.run(run())
