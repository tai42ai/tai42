"""Oracles for the declared-preset-seed applier.

A plugin declares a default preset as a :class:`PresetSeed` at import time; the
startup/reload handler :func:`apply_preset_seeds` creates it when absent (tagging the
active version ``shipped-default`` and seeding its tool_meta), upgrades it when a
shipped default drifts, never touches an operator-edited preset, is idempotent across
re-runs, and raises loudly on a real failure. These drive the applier over the REAL
preset + tool_meta ops against the stateful in-memory fakes (the ``pg`` versioning
fake + the autouse tool_meta fake), so a seeded preset is created, registered LIVE,
and its version tag + display metadata land end-to-end, not mocked.

The registry unit oracle pins the duplicate-name guard in isolation.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from tai42_contract.presets import PresetSeed, PresetSeedToolMeta
from tai42_contract.presets.errors import PresetNotFoundError
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.presets.seeds import PresetSeedRegistry
from tai42_skeleton.presets.store import PresetStoreView

from ..versioning.conftest import FakeVersioningPg

_SEED_LOGGER = "tai42_skeleton.operations.presets"

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [
        {
            "title": "fx",
            "module": "tests.presets._fixtures",
            "include": ["weather", "echo", "plan_tool", "boom_tool"],
        }
    ],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


# -- fixtures ----------------------------------------------------------------


@pytest.fixture
def pg(monkeypatch) -> FakeVersioningPg:
    fake = FakeVersioningPg()

    @asynccontextmanager
    async def fake_client_ctx(client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield fake

    monkeypatch.setattr(store_module, "client_ctx", fake_client_ctx)
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", "secret")
    return fake


@pytest.fixture(autouse=True)
def _reset_preset_registry():
    """Tear down every runtime-registered / quarantined preset after each test — the
    singleton ``PresetManager`` outlives one ``app_context``."""
    yield
    mgr = instance.app.preset_manager

    async def _clear() -> None:
        for name in list(mgr.registered_names()):
            await mgr.remove(name)
        provider = instance.app._fast_mcp.local_provider
        for tool in list(await provider.list_tools()):
            provider.remove_tool(tool.name)

    asyncio.run(_clear())
    for name in list(mgr.quarantined_names()):
        mgr.drop_quarantine(name)


# -- helpers -----------------------------------------------------------------


async def _active_version(name: str):
    """The current version row for ``name`` (the ``is_current`` projection)."""
    versions = await instance.app.presets.store.list_versions(name)
    return next(version for version in versions if version.is_current)


# -- registry: duplicate-name guard ------------------------------------------


def test_seed_registry_rejects_duplicate_name() -> None:
    registry = PresetSeedRegistry()
    registry.register(PresetSeed(name="echo_default", description="d", base_tool="echo"))
    # A second declaration under the same name is a loud programming error — a silent
    # overwrite could drop one plugin's default under another's.
    with pytest.raises(ValueError, match="already registered"):
        registry.register(PresetSeed(name="echo_default", description="d2", base_tool="weather"))


def test_seed_registry_all_preserves_order_and_reset_clears() -> None:
    registry = PresetSeedRegistry()
    registry.register(PresetSeed(name="a", description="d", base_tool="echo"))
    registry.register(PresetSeed(name="b", description="d", base_tool="weather"))
    assert [seed.name for seed in registry.all()] == ["a", "b"]
    registry.reset()
    assert registry.all() == []


# -- fresh boot: create + LIVE + tag + tool_meta -----------------------------


def test_fresh_boot_creates_seed_live_tagged_and_applies_tool_meta(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            seed = PresetSeed(
                name="echo_default",
                description="the shipped echo preset",
                base_tool="echo",
                fixed_kwargs={"text": "hello"},
                tool_meta=PresetSeedToolMeta(display_name="Echo Bot", tags=["featured"], folder_path="acme/echoes"),
            )
            instance.app.presets.register_seed(seed)

            await preset_ops.apply_preset_seeds()

            # The preset exists in the store...
            record = await instance.app.presets.store.get_preset("echo_default")
            assert record.name == "echo_default"
            # ...is registered LIVE (resolvable in the tool registry the SAME epoch)...
            assert "echo_default" in await instance.app.tools.get_tools()
            assert instance.app.preset_manager.is_registered("echo_default")
            # ...its active version wears the shipped-default tag...
            active = await _active_version("echo_default")
            assert preset_ops._SHIPPED_DEFAULT_TAG in active.tags
            # ...and its display metadata is applied, folder_path resolved to a real id.
            meta = await instance.app.tool_meta.store.get_meta("echo_default")
            assert meta is not None
            assert meta.display_name == "Echo Bot"
            assert meta.tags == ["featured"]
            assert meta.folder_id is not None
            # The leaf id is a real folder named ``echoes`` nested under ``acme`` — a
            # resolved id, never a raw path string.
            folders = {f.id: f for f in await instance.app.tool_meta.store.list_folders()}
            leaf = folders[meta.folder_id]
            assert leaf.name == "echoes"
            assert leaf.parent_id is not None
            assert folders[leaf.parent_id].name == "acme"

    asyncio.run(run())


# -- idempotence: re-run is a no-op ------------------------------------------


def test_rerun_is_a_noop(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            seed = PresetSeed(
                name="echo_default",
                description="the shipped echo preset",
                base_tool="echo",
                fixed_kwargs={"text": "hello"},
            )
            instance.app.presets.register_seed(seed)

            await preset_ops.apply_preset_seeds()
            await preset_ops.apply_preset_seeds()
            await preset_ops.apply_preset_seeds()

            # No new version was saved across re-runs — the content already matches the
            # shipped default, so the applier is a pure no-op.
            versions = await instance.app.presets.store.list_versions("echo_default")
            assert len(versions) == 1

    asyncio.run(run())


# -- drift: content change upgrades to a new shipped-default version ----------


def test_content_change_upgrades_new_shipped_default_version(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            v1 = PresetSeed(
                name="weather_default",
                description="v1 description",
                base_tool="weather",
                fixed_kwargs={"units": "metric"},
            )
            instance.app.presets.register_seed(v1)
            await preset_ops.apply_preset_seeds()

            # A new build ships drifted content (different description AND fixed_kwargs)
            # under the same seed name — re-declare it on a clean registry.
            instance.app._seed_registry.reset()
            v2 = PresetSeed(
                name="weather_default",
                description="v2 description",
                base_tool="weather",
                fixed_kwargs={"units": "imperial"},
            )
            instance.app.presets.register_seed(v2)
            await preset_ops.apply_preset_seeds()

            # The drift saved a NEW version, active, tagged shipped-default, carrying v2's
            # content.
            versions = await instance.app.presets.store.list_versions("weather_default")
            assert len(versions) == 2
            active = await _active_version("weather_default")
            assert active.version == 2
            assert preset_ops._SHIPPED_DEFAULT_TAG in active.tags
            body = await instance.app.presets.store.get_active_body("weather_default")
            assert body.description == "v2 description"
            assert body.fixed_kwargs == {"units": "imperial"}

    asyncio.run(run())


# -- same-base upgrade tags atomically: no untagged window to strand ----------


def test_same_base_upgrade_tags_new_version_atomically(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            v1 = PresetSeed(
                name="weather_default",
                description="v1 description",
                base_tool="weather",
                fixed_kwargs={"units": "metric"},
            )
            instance.app.presets.register_seed(v1)
            await preset_ops.apply_preset_seeds()

            # A new build ships drifted content under the SAME base tool (the non-repoint
            # upgrade branch).
            instance.app._seed_registry.reset()
            v2 = PresetSeed(
                name="weather_default",
                description="v2 description",
                base_tool="weather",
                fixed_kwargs={"units": "imperial"},
            )
            instance.app.presets.register_seed(v2)

            # The same-base upgrade tags the new version in the SAME save commit — it must
            # NEVER reach the standalone tag door. A separate post-save tag step is exactly
            # the untagged window an interrupted upgrade could strand (the next boot would
            # read it as operator-edited and freeze the preset), and it no longer exists.
            async def forbidden_set_tags(name, version, tags):
                raise AssertionError("same-base upgrade must tag atomically, not via set_version_tags")

            monkeypatch.setattr(instance.app.presets, "set_version_tags", forbidden_set_tags)

            await preset_ops.apply_preset_seeds()

            # The new active version carries v2's content and already wears the tag — the tag
            # rode the save commit, so no untagged active version can be left behind.
            versions = await instance.app.presets.store.list_versions("weather_default")
            assert len(versions) == 2
            active = await _active_version("weather_default")
            assert active.version == 2
            assert preset_ops._SHIPPED_DEFAULT_TAG in active.tags
            body = await instance.app.presets.store.get_active_body("weather_default")
            assert body.description == "v2 description"
            assert body.fixed_kwargs == {"units": "imperial"}

    asyncio.run(run())


# -- operator-edited (untagged) active version is never touched --------------


def test_operator_edited_untouched_with_visible_skip(pg, caplog) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            seed = PresetSeed(
                name="weather_default",
                description="shipped description",
                base_tool="weather",
                fixed_kwargs={"units": "metric"},
            )
            instance.app.presets.register_seed(seed)
            await preset_ops.apply_preset_seeds()

            # An operator saves a new version — the save door tags nothing, so the active
            # version is UNtagged (operator-edited).
            await preset_ops.save_version(
                name="weather_default",
                fixed_kwargs={"units": "operator"},
                extensions=None,
                output_schema=None,
                output_schema_provided=False,
                description="operator description",
            )
            active_before = await _active_version("weather_default")
            assert preset_ops._SHIPPED_DEFAULT_TAG not in active_before.tags

            with caplog.at_level(logging.INFO, logger=_SEED_LOGGER):
                await preset_ops.apply_preset_seeds()

            # The applier left the operator's version untouched — no new version, the
            # active body still the operator's — and surfaced a VISIBLE skip line naming
            # the preset and why.
            versions = await instance.app.presets.store.list_versions("weather_default")
            assert len(versions) == 2
            body = await instance.app.presets.store.get_active_body("weather_default")
            assert body.description == "operator description"
            assert body.fixed_kwargs == {"units": "operator"}
            assert any(
                "weather_default" in rec.getMessage() and "operator-edited" in rec.getMessage()
                for rec in caplog.records
            )

    asyncio.run(run())


# -- create tags version 1 atomically: no untagged window to strand ----------


def test_seed_create_tags_version_one_atomically(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            seed = PresetSeed(
                name="echo_default",
                description="the shipped echo preset",
                base_tool="echo",
                fixed_kwargs={"text": "hello"},
                tool_meta=PresetSeedToolMeta(display_name="Echo Bot"),
            )
            instance.app.presets.register_seed(seed)

            # The create tags version 1 in the SAME store commit — it must NEVER reach the
            # standalone tag door. A separate post-create tag step is exactly the untagged
            # window an interrupted create could strand, and it no longer exists.
            async def forbidden_set_tags(name, version, tags):
                raise AssertionError("seed create must tag version 1 atomically, not via set_version_tags")

            monkeypatch.setattr(instance.app.presets, "set_version_tags", forbidden_set_tags)

            await preset_ops.apply_preset_seeds()

            # Version 1 is the only version and already wears the shipped-default tag — the tag
            # rode the create commit, so no untagged version can be left behind.
            versions = await instance.app.presets.store.list_versions("echo_default")
            assert len(versions) == 1
            active = await _active_version("echo_default")
            assert active.version == 1
            assert preset_ops._SHIPPED_DEFAULT_TAG in active.tags
            # tool_meta still applied on the atomic create path.
            meta = await instance.app.tool_meta.store.get_meta("echo_default")
            assert meta is not None
            assert meta.display_name == "Echo Bot"

    asyncio.run(run())


# -- blank seed display_name is refused loudly, never a blank label -----------


def test_blank_display_name_seed_refused_loudly(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A whitespace-only display_name must be refused at the SAME door guard the overlay
            # upsert enforces — never persisted as an empty label.
            seed = PresetSeed(
                name="echo_default",
                description="d",
                base_tool="echo",
                tool_meta=PresetSeedToolMeta(display_name="   "),
            )
            instance.app.presets.register_seed(seed)

            with pytest.raises(BadRequestError, match="display_name must not be blank"):
                await preset_ops.apply_preset_seeds()

            # No blank label was persisted for the preset.
            meta = await instance.app.tool_meta.store.get_meta("echo_default")
            assert meta is None or meta.display_name is None

    asyncio.run(run())


# -- base_tool re-point ships as an upgrade (the live binding changes) --------


def test_base_tool_change_upgrades_and_repoints_live_binding(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            v1 = PresetSeed(name="repoint_default", description="same", base_tool="echo")
            instance.app.presets.register_seed(v1)
            await preset_ops.apply_preset_seeds()

            # A new build re-points ONLY the base_tool (description/kwargs/schemas unchanged) —
            # base_tool is content, so this must register as drift and ship.
            instance.app._seed_registry.reset()
            v2 = PresetSeed(name="repoint_default", description="same", base_tool="weather")
            instance.app.presets.register_seed(v2)
            await preset_ops.apply_preset_seeds()

            # A new tagged version carrying the NEW base_tool is active, and the live tool
            # re-bound onto it (history preserved, not a recreate).
            versions = await instance.app.presets.store.list_versions("repoint_default")
            assert len(versions) == 2
            active = await _active_version("repoint_default")
            assert active.version == 2
            assert preset_ops._SHIPPED_DEFAULT_TAG in active.tags
            body = await instance.app.presets.store.get_active_body("repoint_default")
            assert body.base_tool == "weather"
            assert "repoint_default" in await instance.app.tools.get_tools()

            # Idempotent: the seed now matches, so a further re-run saves nothing.
            await preset_ops.apply_preset_seeds()
            assert len(await instance.app.presets.store.list_versions("repoint_default")) == 2

    asyncio.run(run())


# -- re-point onto a rejecting base is validated + refused, persists nothing ---


def test_repoint_onto_rejecting_base_raises_and_persists_nothing(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            v1 = PresetSeed(name="repoint_default", description="same", base_tool="echo")
            instance.app.presets.register_seed(v1)
            await preset_ops.apply_preset_seeds()

            # The target base tool refuses any body it is asked to persist — the SAME write
            # validator the create/save doors honor.
            async def validator(body):
                return ["weather refuses this preset"]

            instance.app.presets.register_write_validator("weather", validator)

            # A new build re-points onto that rejecting base.
            instance.app._seed_registry.reset()
            v2 = PresetSeed(name="repoint_default", description="same", base_tool="weather")
            instance.app.presets.register_seed(v2)

            # The re-point runs the shared authoring validators BEFORE its direct store write,
            # so the rejecting base raises loudly — never persisted and served unvalidated.
            with pytest.raises(preset_ops.BadRequestError, match="weather refuses this preset"):
                await preset_ops.apply_preset_seeds()

            # Nothing persisted or rebound: no new version, the active body still v1 on echo.
            versions = await instance.app.presets.store.list_versions("repoint_default")
            assert len(versions) == 1
            body = await instance.app.presets.store.get_active_body("repoint_default")
            assert body.base_tool == "echo"

    asyncio.run(run())


# -- re-point onto an existing preset name is rejected (no preset-on-preset) ---


def test_repoint_onto_a_preset_base_raises_and_persists_nothing(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A shipped preset that will be (illegally) named as another seed's base tool —
            # a preset can never be another preset's base (create forbids it; chaining makes
            # rehydration order-dependent).
            base_preset = PresetSeed(name="echo_default", description="d", base_tool="echo")
            instance.app.presets.register_seed(base_preset)
            v1 = PresetSeed(name="repoint_default", description="same", base_tool="echo")
            instance.app.presets.register_seed(v1)
            await preset_ops.apply_preset_seeds()

            # A new build re-points onto the OTHER preset's name — the dry-run bind binds via
            # ``get_tool`` and a preset IS a registered tool, so only the create-mirrored
            # preset-base guard on the re-point path catches it.
            instance.app._seed_registry.reset()
            instance.app.presets.register_seed(PresetSeed(name="echo_default", description="d", base_tool="echo"))
            v2 = PresetSeed(name="repoint_default", description="same", base_tool="echo_default")
            instance.app.presets.register_seed(v2)

            with pytest.raises(BadRequestError, match="is itself a preset"):
                await preset_ops.apply_preset_seeds()

            # Nothing persisted or rebound: no new version, the active body still v1 on echo.
            versions = await instance.app.presets.store.list_versions("repoint_default")
            assert len(versions) == 1
            body = await instance.app.presets.store.get_active_body("repoint_default")
            assert body.base_tool == "echo"

    asyncio.run(run())


# -- re-point ROLLBACK: a reload failure restores the store pointer + old bind -


def test_repoint_reload_failure_rolls_back_store_and_binding(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            v1 = PresetSeed(name="repoint_default", description="same", base_tool="echo")
            instance.app.presets.register_seed(v1)
            await preset_ops.apply_preset_seeds()
            assert "repoint_default" in await instance.app.tools.get_tools()

            instance.app._seed_registry.reset()
            v2 = PresetSeed(name="repoint_default", description="same", base_tool="weather")
            instance.app.presets.register_seed(v2)

            # The re-point store write lands, then the reload/bind onto the new base fails.
            async def flaky_reload(name):
                raise RuntimeError("reload onto the new base crashed")

            monkeypatch.setattr(instance.app.preset_manager, "reload", flaky_reload)

            with pytest.raises(RuntimeError, match="reload onto the new base crashed"):
                await preset_ops.apply_preset_seeds()

            # The store pointer rolled back to the prior version (v1, echo) — no new active
            # version is served, so store + live never diverge.
            active = await _active_version("repoint_default")
            assert active.version == 1
            body = await instance.app.presets.store.get_active_body("repoint_default")
            assert body.base_tool == "echo"
            # The reload raised before touching the binding, so the live tool stayed bound to
            # the OLD base — it still resolves in the registry, unchanged.
            assert "repoint_default" in await instance.app.tools.get_tools()

    asyncio.run(run())


# -- concurrent-boot dedup: a sibling upgrade already applied → no-op ----------


def test_upgrade_dedups_when_sibling_already_applied(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            v1 = PresetSeed(
                name="weather_default",
                description="v1 description",
                base_tool="weather",
                fixed_kwargs={"units": "metric"},
            )
            instance.app.presets.register_seed(v1)
            await preset_ops.apply_preset_seeds()
            # The active body this worker read BEFORE a sibling raced it (the stale drift view).
            stale_active = await instance.app.presets.store.get_active_body("weather_default")

            # A sibling worker applies the same drift first — v2 becomes the active shipped
            # default (two versions).
            instance.app._seed_registry.reset()
            v2 = PresetSeed(
                name="weather_default",
                description="v2 description",
                base_tool="weather",
                fixed_kwargs={"units": "imperial"},
            )
            instance.app.presets.register_seed(v2)
            await preset_ops.apply_preset_seeds()
            assert len(await instance.app.presets.store.list_versions("weather_default")) == 2

            # This worker entered _seed_upgrade with the STALE pre-sibling active body. The
            # dedup re-read sees the sibling's v2 already active and no-ops — no third version,
            # so a concurrent fleet boot yields ONE upgraded version, not a duplicate per worker.
            await preset_ops._seed_upgrade(v2, stale_active)
            assert len(await instance.app.presets.store.list_versions("weather_default")) == 2

    asyncio.run(run())


# -- concurrent-boot dedup: a sibling create loads locally (callable first boot) --


def test_sibling_create_dedup_loads_seed_into_local_registry(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            seed = PresetSeed(
                name="echo_default",
                description="the shipped echo preset",
                base_tool="echo",
                fixed_kwargs={"text": "hello"},
            )
            instance.app.presets.register_seed(seed)

            # A sibling worker wins the create race: the row lives in the store, but its
            # tool-registry register never fanned out to THIS worker. Model that by
            # creating the seed, then tearing down only this worker's local registration —
            # the store row stays, the live tool does not.
            await preset_ops.apply_preset_seeds()
            await instance.app.preset_manager.remove("echo_default")
            assert not instance.app.preset_manager.is_registered("echo_default")
            assert "echo_default" not in await instance.app.tools.get_tools()

            # Force the sibling-create-dedup path: this worker's opening presence check
            # misses (the row is not in its view yet), its create then conflicts on the
            # sibling's row, and the re-read confirms present. Patch the store-view CLASS —
            # the app rebuilds the view per access, so an instance patch would not stick.
            real_get_preset = PresetStoreView.get_preset
            calls = {"n": 0}

            async def flaky_get_preset(self, name):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PresetNotFoundError(name)
                return await real_get_preset(self, name)

            monkeypatch.setattr(PresetStoreView, "get_preset", flaky_get_preset)

            await preset_ops._apply_one_seed(seed)

            # The unified local-load guard bound the store-active version into THIS worker's
            # registry — the seed is callable on first boot, before any reload.
            assert instance.app.preset_manager.is_registered("echo_default")
            assert "echo_default" in await instance.app.tools.get_tools()
            # No re-ship: the store still holds the sibling's single tagged version.
            versions = await instance.app.presets.store.list_versions("echo_default")
            assert len(versions) == 1
            active = await _active_version("echo_default")
            assert preset_ops._SHIPPED_DEFAULT_TAG in active.tags

    asyncio.run(run())


# -- concurrent-boot success window: present-in-store but locally absent → guard loads --


def test_present_but_unregistered_seed_loaded_by_guard(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            seed = PresetSeed(
                name="echo_default",
                description="the shipped echo preset",
                base_tool="echo",
                fixed_kwargs={"text": "hello"},
            )
            instance.app.presets.register_seed(seed)

            # The success window: a sibling created the seed AFTER this worker's rehydrate,
            # so the opening presence check SUCCEEDS (row present) yet the tool is not live
            # here. Model it by creating the seed then tearing down only this worker's local
            # registration — the store row + its shipped-default tag stay.
            await preset_ops.apply_preset_seeds()
            await instance.app.preset_manager.remove("echo_default")
            assert not instance.app.preset_manager.is_registered("echo_default")
            assert "echo_default" not in await instance.app.tools.get_tools()

            # get_preset succeeds → the up-to-date no-op branch ships nothing, and the
            # unified guard binds the active stored version so the seed is callable.
            await preset_ops._apply_one_seed(seed)
            assert instance.app.preset_manager.is_registered("echo_default")
            assert "echo_default" in await instance.app.tools.get_tools()
            # No re-ship: still the single tagged version.
            versions = await instance.app.presets.store.list_versions("echo_default")
            assert len(versions) == 1
            active = await _active_version("echo_default")
            assert preset_ops._SHIPPED_DEFAULT_TAG in active.tags

    asyncio.run(run())


# -- resolve_folder_path: a blank / all-blank path raises loudly --------------


def test_folder_path_blank_segment_raises_loudly(pg) -> None:
    from tai42_skeleton.operations.tool_meta import resolve_folder_path

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A blank middle segment is an authoring error, never a silently dropped segment.
            with pytest.raises(BadRequestError, match="folder path segment must not be blank"):
                await resolve_folder_path("acme//echoes")
            # A path that names no real folder is likewise a loud error, never a silent no-op.
            with pytest.raises(BadRequestError, match="folder path segment must not be blank"):
                await resolve_folder_path("///")
            with pytest.raises(BadRequestError, match="folder path segment must not be blank"):
                await resolve_folder_path("")

    asyncio.run(run())


# -- store OFF: visible skip, no raise, nothing created ----------------------


def test_store_off_visible_skip_no_raise(monkeypatch, caplog) -> None:
    # No versioned-document store configured (the OFF posture) — the applier must skip
    # visibly and never raise, and create nothing.
    monkeypatch.delenv("TAI_DATABASE_DEFAULT_PG_PASSWORD", raising=False)

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            seed = PresetSeed(name="echo_default", description="d", base_tool="echo")
            instance.app.presets.register_seed(seed)

            with caplog.at_level(logging.INFO, logger=_SEED_LOGGER):
                await preset_ops.apply_preset_seeds()  # no raise

            assert "echo_default" not in await instance.app.tools.get_tools()
            assert any(
                "echo_default" in rec.getMessage() and "not configured" in rec.getMessage() for rec in caplog.records
            )

    asyncio.run(run())


# -- rejected validation raises loudly at the lifecycle hook -----------------


def test_invalid_seed_raises_loudly(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A seed whose base_tool is not a registered tool cannot bind — the shared
            # create core rejects it, and the applier surfaces the failure LOUDLY at the
            # startup/reload hook rather than swallowing it.
            seed = PresetSeed(name="broken_default", description="d", base_tool="no_such_tool")
            instance.app.presets.register_seed(seed)

            with pytest.raises(BadRequestError, match="not a registered tool"):
                await preset_ops.apply_preset_seeds()

            # Nothing was persisted for the rejected seed.
            with pytest.raises(PresetNotFoundError):
                await instance.app.presets.store.get_preset("broken_default")

    asyncio.run(run())
