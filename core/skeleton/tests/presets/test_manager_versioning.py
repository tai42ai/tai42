"""Preset versioning: the spec map is authoritative and in lockstep, reload and
rollback serve the right kwargs, a concurrent run never sees a gap, the active
version is retained across save/rollback/reload/rehydrate, the run chokepoint
stamps the active version, and save numbering is MAX+1."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.presets.errors import PresetNotFoundError

from tai42_skeleton.app.instance import app
from tai42_skeleton.tools.binding import UnknownToolError

from ._manager_fixtures import FakeVersioningPg, _create_versioned, _manifest


def test_spec_map_serves_active_baked_kwargs(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await mgr.register("eph", "weather", {"units": "eph"}, [], "d")
            await _create_versioned("ver", "weather", {"units": "v1"}, [], description="Version one")

            # The spec map is the source of truth for baked kwargs.
            assert mgr.baked_kwargs("eph") == {"units": "eph"}
            assert mgr.baked_kwargs("ver") == {"units": "v1"}
            assert mgr.get_spec("ver").description == "Version one"
            assert set(mgr.registered_names()) == {"eph", "ver"}

            # The map stays in lockstep with the active version after an edit.
            await app.presets.store.save_version("ver", fixed_kwargs={"units": "v2"})
            await mgr.reload("ver")
            assert mgr.baked_kwargs("ver") == {"units": "v2"}

            with pytest.raises(PresetNotFoundError):
                mgr.get_spec("nope")

    asyncio.run(run())


def test_reload_and_rollback_serve_right_kwargs(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [])

            await store.save_version("wv", fixed_kwargs={"units": "v2"})
            await mgr.reload("wv")
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "v2"}

            await store.rollback("wv", 1)
            await mgr.reload("wv")
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "v1"}

    asyncio.run(run())


def test_reload_never_exposes_a_gap_to_a_concurrent_run(pg: FakeVersioningPg, monkeypatch):
    """A run resolving a preset the instant a version/rollback reload lands must see
    the OLD or the NEW tool, never a transient ``UnknownToolError``.

    The consumer this guards is a backend worker job dispatched on the same serving
    loop as the fan-out reload (``run_tool`` -> ``_resolve_run_target`` -> ``get_tool``):
    a preset ``reload_tool`` is applied inline on that loop and holds no reload gate, so
    a run that observed the rebind mid-teardown got a hard miss the caller does not
    retry. The rebind reads the new body and BINDS the new tool while the old one is
    still live, then swaps with no ``await`` — so every loop turn resolves ``name``.

    A reload that tears the old registration down before its body-read/bind awaits
    lets a run scheduled during those awaits resolve the name to nothing; this test
    schedules exactly that resolver."""

    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [])
            await store.save_version("wv", fixed_kwargs={"units": "v2"})

            # Make the reload YIELD the loop during its body read, so a concurrent
            # resolver is scheduled mid-reload: with teardown before this read, the
            # yields would expose an unregistered window.
            real_read = app.presets.get_active_versioned_body
            entered = asyncio.Event()

            async def yielding_read(name: str):
                entered.set()
                for _ in range(5):
                    await asyncio.sleep(0)
                return await real_read(name)

            monkeypatch.setattr(app.presets, "get_active_versioned_body", yielding_read)

            misses: list[str] = []
            stop = asyncio.Event()

            async def resolver() -> None:
                await entered.wait()
                while not stop.is_set():
                    try:
                        await app.tools.get_tool("wv")
                    except UnknownToolError:
                        misses.append("wv")
                    await asyncio.sleep(0)

            res_task = asyncio.create_task(resolver())
            await mgr.reload("wv")
            stop.set()
            await res_task

            assert misses == [], "a concurrent run resolved the preset to a gap during a reload"
            # The swap still lands the new active body.
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "v2"}

    asyncio.run(run())


def test_active_version_retained_across_create_save_rollback_reload(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            # A fresh create mints version 1 and the engine retains it beside the body.
            await _create_versioned("wv", "weather", {"units": "v1"}, [])
            assert mgr.active_version("wv") == 1
            # A plain (non-preset) tool has no retained version.
            assert mgr.active_version("weather") is None

            # A save + reload advances the retained version to the new active one.
            await store.save_version("wv", fixed_kwargs={"units": "v2"})
            await mgr.reload("wv")
            assert mgr.active_version("wv") == 2

            # A rollback + reload re-points the retained version at the rolled-back one
            # (the active pointer, not MAX).
            await store.rollback("wv", 1)
            await mgr.reload("wv")
            assert mgr.active_version("wv") == 1

            # Teardown drops the retained version in lockstep with the spec.
            await mgr.remove("wv")
            assert mgr.active_version("wv") is None

    asyncio.run(run())


def test_active_version_survives_rehydrate(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [])
            await store.save_version("wv", fixed_kwargs={"units": "v2"})
            await mgr.reload("wv")
            assert mgr.active_version("wv") == 2

            # A reload_config wipe + rehydrate rebuilds the version from the store's
            # single version-aware batched read — the retained value is not lost.
            await mgr.remove("wv")
            await mgr.rehydrate()
            assert mgr.active_version("wv") == 2

    asyncio.run(run())


def test_run_tool_stamps_registered_preset_with_active_version(pg: FakeVersioningPg, monkeypatch):
    """The run chokepoint attributes a registered preset dispatch with its version;
    a plain tool is left unstamped."""

    class _SpyWriter:
        def __init__(self) -> None:
            self.stamps: list[dict] = []

        def current_trace_id(self):
            return "t"

        def trace_attributes(self, *, name, tags, metadata, user_id=None, session_id=None):
            self.stamps.append({"tags": tags, "metadata": metadata})
            from contextlib import nullcontext

            return nullcontext()

    class _SpyMonitoring:
        def __init__(self, writer) -> None:
            self.writer = writer

    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [])
            await store.save_version("wv", fixed_kwargs={"units": "v2"})
            await mgr.reload("wv")

            writer = _SpyWriter()
            import tai42_skeleton.monitoring as monitoring_mod

            monkeypatch.setattr(monitoring_mod, "get_monitoring", lambda: _SpyMonitoring(writer))

            # A registered-preset dispatch stamps preset:/preset-v: with the active version.
            await app.tools.run_tool("wv", {"city": "x"})
            preset_stamps = [s for s in writer.stamps if any(t.startswith("preset:") for t in (s["tags"] or []))]
            assert len(preset_stamps) == 1
            assert preset_stamps[0]["tags"] == ["preset:wv", "preset-v:2"]
            assert preset_stamps[0]["metadata"]["preset_version"] == "2"

            # A plain (non-preset) tool dispatch stamps NO preset attribution.
            writer.stamps.clear()
            await app.tools.run_tool("weather", {"city": "x", "units": "z"})
            assert not [s for s in writer.stamps if any(t.startswith("preset:") for t in (s["tags"] or []))]

    asyncio.run(run())


def test_save_version_numbering_is_max_plus_one_post_rollback(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            store = app.presets.store
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v1"}, [])
            for units in ("v2", "v3", "v4", "v5"):
                await store.save_version("wv", fixed_kwargs={"units": units})
            await store.rollback("wv", 2)  # active trails MAX
            new = await store.save_version("wv", fixed_kwargs={"units": "v6"})
            assert new.version == 6  # MAX+1, not active(2)+1
            await mgr.reload("wv")
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "v6"}

    asyncio.run(run())
