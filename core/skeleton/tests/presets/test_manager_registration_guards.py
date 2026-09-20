"""Preset registration guards: a name conflict raises before any store write, an
invalid name is rejected, a failed register leaves no partial registration, and an
edit-path reload failure restores the old registration."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.agent.base import PresetSpec
from tai42_contract.presets.errors import PresetNameConflictError

from tai42_skeleton.app.instance import app
from tai42_skeleton.exceptions.exceptions import TaiValidationError

from ._manager_fixtures import FakeVersioningPg, _create_versioned, _manifest


def test_name_conflict_raises_before_store_write(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            # "weather" is a live non-preset base tool.
            assert await app.preset_manager.name_conflicts("weather") is True
            with pytest.raises(PresetNameConflictError):
                await app.presets.store.create_preset(
                    PresetSpec(name="weather", description="d", base_tool="echo", fixed_kwargs={}),
                    extensions=[],
                )
            # No preset row persisted (boot-seeded role documents are a different kind).
            assert [d for d in pg.documents if d["kind"] == "preset"] == []

    asyncio.run(run())


def test_register_rejects_invalid_name(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            # A preset name is a live tool name + a route segment, so the manager
            # rejects a name outside the tool-name-safe alphabet/length before any
            # bind (the create route's 400 guard shares this rule).
            for bad in ("a/b", "x" * 65, "bad name"):
                with pytest.raises(ValueError, match="invalid preset name"):
                    await app.preset_manager.register(bad, "echo", {}, [], "d")

    asyncio.run(run())


def test_register_failure_leaves_no_partial_registration(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # An unknown extension makes the branch bind raise inside register.
            with pytest.raises(TaiValidationError):
                await mgr.register("bad", "echo", {}, [["ghost_ext"]], "d")
            tools = set(await app.tools.get_tools())
            assert "bad" not in tools
            assert not mgr.is_registered("bad")
            # The structured-registry seed was rolled back too.
            assert list(app._tool_registry.tool_extensions_iterator("bad")) == []

    asyncio.run(run())


def test_edit_path_reload_failure_restores_old_registration(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "echo", {}, [["exta"]])
            assert await app.tools.run_tool("wv_exta", {"text": "hi"}) == "hi|a"

            # A new version whose extensions reference an unknown ext will fail the
            # reload's re-register; the committed store bump is NOT unwound.
            await store.save_version("wv", extensions=[["ghost_ext"]])
            with pytest.raises(TaiValidationError):
                await mgr.reload("wv")

            # The PRIOR registration survived: base AND its branch still runnable,
            # and the spec map still holds the old body (the restore ran).
            assert await app.tools.run_tool("wv", {"text": "hi"}) == "hi"
            assert await app.tools.run_tool("wv_exta", {"text": "hi"}) == "hi|a"
            assert mgr.get_spec("wv").extensions == [["exta"]]

    asyncio.run(run())
