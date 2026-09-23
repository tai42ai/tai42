"""Preset rehydrate durability and quarantine: rehydrate re-registers only
store-backed presets, skips a record whose active body is absent, quarantines the
three stale-preset causes, self-registers idempotently, stores and resets a
quarantine reason, and quarantines on a reconcile re-register failure."""

from __future__ import annotations

import asyncio
import logging

from tai42_contract.agent.base import PresetSpec
from tai42_contract.presets import PresetBody

from tai42_skeleton.app.instance import app
from tai42_skeleton.exceptions.exceptions import TaiValidationError

from ._manager_fixtures import FakeVersioningPg, _create_versioned, _live_tool_names, _manifest


def test_rehydrate_reregisters_only_store_backed_presets(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # A registration with no store row (bound directly, never persisted)
            # alongside a persisted one.
            await mgr.register("unbacked", "weather", {"units": "e"}, [], "d")
            await _create_versioned("ver", "weather", {"units": "v"}, [])

            # Simulate reload_config wiping every runtime preset, then rehydrate.
            for name in ("unbacked", "ver"):
                await mgr.remove(name)
            await mgr.rehydrate()

            tools = set(await app.tools.get_tools())
            assert "ver" in tools
            assert "unbacked" not in tools
            assert set(mgr.registered_names()) == {"ver"}  # only the store-backed one rebuilt
            assert await app.tools.run_tool("ver", {"city": "x"}) == {"city": "x", "units": "v"}

    asyncio.run(run())


def test_rehydrate_skips_record_whose_active_body_is_absent(pg: FakeVersioningPg, monkeypatch, caplog):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # Two persisted presets. ``list_presets`` and ``list_active_versioned_bodies``
            # are two separate store reads, so a delete landing between them leaves a
            # record whose active body is already gone. Model that read-skew: both
            # records survive in ``list_presets`` while ``absent`` is dropped from
            # the active version+body map.
            await _create_versioned("present", "weather", {"units": "v"}, [])
            await _create_versioned("absent", "weather", {"units": "g"}, [])

            # Simulate reload_config wiping every runtime preset before rehydrate.
            for name in ("present", "absent"):
                await mgr.remove(name)

            real_bodies = app.presets.list_active_versioned_bodies

            async def _bodies_without_absent():
                bodies = await real_bodies()
                bodies.pop("absent", None)
                return bodies

            monkeypatch.setattr(app.presets, "list_active_versioned_bodies", _bodies_without_absent)

            # A record with no active body must be SKIPPED, never a bare
            # ``bodies[rec.name]`` KeyError that aborts the whole boot/reload.
            with caplog.at_level(logging.WARNING, logger="tai42_skeleton.presets.manager"):
                await mgr.rehydrate()

            # The skipped record is neither registered nor quarantined — just dropped.
            assert not mgr.is_registered("absent")
            assert not mgr.is_quarantined("absent")
            assert "absent" not in await app.tools.get_tools()
            # The present-body preset rebuilt normally and stays runnable.
            assert mgr.is_registered("present")
            assert await app.tools.run_tool("present", {"city": "x"}) == {"city": "x", "units": "v"}
            # The skip was logged loudly for the missing name.
            assert "absent" in caplog.text

    asyncio.run(run())


def test_rehydrate_quarantines_foreign_name(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # Seed a persisted preset whose NAME is a live base tool. The store's
            # create-time collision guard (rightly) blocks this via the preset
            # view, so seed through the generic store to model a name that only
            # BECAME a base tool after the preset was persisted.
            body = PresetBody(base_tool="echo", description="d", fixed_kwargs={}, extensions=[])
            await app.versioning.store.create("preset", "weather", body.model_dump())
            await mgr.rehydrate()  # app still boots — no raise

            assert mgr.is_quarantined("weather")
            # Not registered as a preset; the foreign base tool still owns the name.
            assert not mgr.is_registered("weather")
            assert await app.tools.run_tool("weather", {"city": "x"}) == {"city": "x", "units": "metric"}

            # The DELETE-conflicted branch drops the quarantine entry immediately.
            mgr.drop_quarantine("weather")
            assert not mgr.is_quarantined("weather")

    asyncio.run(run())


def test_rehydrate_quarantines_missing_base_tool(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await app.presets.store.create_preset(
                PresetSpec(name="orphan", description="d", base_tool="gone_tool", fixed_kwargs={}), extensions=[]
            )
            await mgr.rehydrate()
            assert mgr.is_quarantined("orphan")
            assert not mgr.is_registered("orphan")
            assert "orphan" not in await app.tools.get_tools()

    asyncio.run(run())


def test_rehydrate_quarantines_preset_owned_base(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # A valid versioned preset, plus another whose base_tool is that preset
            # — a preset may not be another preset's base, in EITHER load order.
            await _create_versioned("base_preset", "weather", {"units": "v"}, [])
            await app.presets.store.create_preset(
                PresetSpec(name="chained", description="d", base_tool="base_preset", fixed_kwargs={}), extensions=[]
            )
            await mgr.remove("base_preset")  # clear runtime state before the rehydrate
            await mgr.rehydrate()

            assert mgr.is_registered("base_preset")  # the legitimate one rebuilt
            assert mgr.is_quarantined("chained")  # the preset-on-preset rejected
            assert not mgr.is_registered("chained")

    asyncio.run(run())


def test_rehydrate_idempotent_self_registration_no_conflict(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v"}, [["exta"]])
            # A second rehydrate (as a redundant reload would trigger) rebuilds the
            # same preset cleanly — no conflict, still runnable, exactly one branch.
            await mgr.rehydrate()
            assert mgr.is_registered("wv")
            assert not mgr.is_quarantined("wv")
            assert await app.tools.run_tool("wv", {"city": "x"}) == {"city": "x", "units": "v"}
            assert _live_tool_names().count("wv_exta") == 1

    asyncio.run(run())


def test_quarantine_reason_readable_and_cleared(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            # A stored preset whose name is a live base tool quarantines on rehydrate.
            body = PresetBody(base_tool="echo", description="d", fixed_kwargs={}, extensions=[])
            await app.versioning.store.create("preset", "weather", body.model_dump())
            await mgr.rehydrate()

            assert mgr.is_quarantined("weather")
            reason = mgr.quarantine_reason("weather")
            assert reason is not None
            assert "occupied by an existing tool" in reason
            # A non-quarantined / unknown name carries no reason.
            assert mgr.quarantine_reason("nope") is None
            # Drop clears BOTH membership and the reason.
            mgr.drop_quarantine("weather")
            assert not mgr.is_quarantined("weather")
            assert mgr.quarantine_reason("weather") is None

    asyncio.run(run())


def test_quarantine_reason_bulk_reset_is_coherent(pg: FakeVersioningPg):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await app.presets.store.create_preset(
                PresetSpec(name="orphan", description="d", base_tool="gone_tool", fixed_kwargs={}), extensions=[]
            )
            await mgr.rehydrate()
            assert "gone_tool" in (mgr.quarantine_reason("orphan") or "")

            # A second rehydrate wipes the map wholesale then rebuilds it — still
            # quarantined, same reason, no stale entry accreted.
            await mgr.rehydrate()
            assert mgr.is_quarantined("orphan")
            assert "gone_tool" in (mgr.quarantine_reason("orphan") or "")
            assert set(mgr.quarantined_names()) == {"orphan"}

    asyncio.run(run())


def test_reconcile_quarantines_on_reregister_failure(pg: FakeVersioningPg, monkeypatch):
    async def run():
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await _create_versioned("wv", "weather", {"units": "v"}, [])

            # The base tool is still live, but re-registration fails (the environment
            # changed after the reload) — the preset is quarantined, never left
            # half-bound to a stale closure.
            async def _boom(*args, **kwargs):
                raise TaiValidationError("reconcile re-register failure")

            monkeypatch.setattr(mgr, "_register", _boom)
            await mgr.reconcile_bases({"weather"})

            assert mgr.is_quarantined("wv")
            assert not mgr.is_registered("wv")

    asyncio.run(run())


def test_rehydrate_quarantines_a_preset_whose_secret_reference_is_unset(pg: FakeVersioningPg, monkeypatch, caplog):
    async def run():
        # A stored preset whose ``fixed_kwargs`` references an env var that is not set:
        # the bind at rehydrate raises loudly, so the preset is quarantined (never a
        # silent empty bake, never a boot crash) and the reason names the missing var.
        monkeypatch.delenv("TAI_TEST_ABSENT_SECRET", raising=False)
        async with app.app_context(_manifest()):
            mgr = app.preset_manager
            await app.presets.store.create_preset(
                PresetSpec(
                    name="secretive",
                    description="d",
                    base_tool="weather",
                    fixed_kwargs={"units": "!ENV ${TAI_TEST_ABSENT_SECRET}"},
                ),
                extensions=[],
            )
            with caplog.at_level(logging.ERROR, logger="tai42_skeleton.presets.manager"):
                await mgr.rehydrate()
            assert mgr.is_quarantined("secretive")
            assert not mgr.is_registered("secretive")
            assert "secretive" not in await app.tools.get_tools()
            assert "TAI_TEST_ABSENT_SECRET" in caplog.text

    asyncio.run(run())
