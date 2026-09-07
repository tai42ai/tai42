"""Oracles for the declared-preset-seed applier.

A plugin declares a default preset as a :class:`PresetSeed` at import time; the
startup/reload handler :func:`apply_preset_seeds` creates it when absent (seeding its
tool_meta), leaves a preset already present untouched, is idempotent across re-runs, and
raises loudly on a real failure. These drive the applier over the REAL preset + tool_meta
ops against the stateful in-memory fakes (the ``pg`` versioning fake + the autouse
tool_meta fake), so a seeded preset is created, registered LIVE, and its display metadata
lands end-to-end, not mocked.

The per-seed advisory lock the applier takes is faked in-process too
(:class:`_FakeAdvisoryLocks`), so the concurrency oracle drives two appliers through a
CHOSEN interleaving — the one that loses the overlay when the appliers are not
serialized — with no database.

The registry unit oracle pins the duplicate-name guard in isolation.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

import pytest
from tai42_contract.presets import PresetSeed, PresetSeedToolMeta
from tai42_contract.presets.errors import PresetNotFoundError
from tai42_contract.tool_meta import FolderNameConflictError
from tai42_kit.clients.impl.postgres import PostgresClient

import tai42_skeleton.db.locks as locks_module
import tai42_skeleton.versioning.store as store_module
from tai42_skeleton.app import instance
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import BadRequestError
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.presets.seeds import PresetSeedRegistry
from tai42_skeleton.presets.store import PresetStoreView
from tai42_skeleton.tool_meta.store import PostgresToolMetaStore

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


class _FakeAdvisoryLocks:
    """An in-process stand-in for PostgreSQL transaction-scoped advisory locks.

    One :class:`asyncio.Lock` per key, acquired by the ``pg_advisory_xact_lock``
    statement and released when the holder's connection context exits — the scope the
    real transaction-scoped lock has. ``contended`` is set the moment an acquire has to
    WAIT, which is how a test observes that a second applier is serialized behind the
    first rather than running through the same window.
    """

    def __init__(self) -> None:
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.contended = asyncio.Event()

    @asynccontextmanager
    async def client_ctx(self, client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield _FakeLockPool(self)

    async def acquire(self, key: tuple[int, int]) -> asyncio.Lock:
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            self.contended.set()
        await lock.acquire()
        return lock


class _FakeLockPool:
    def __init__(self, locks: _FakeAdvisoryLocks) -> None:
        self._locks = locks

    @asynccontextmanager
    async def connection(self):
        conn = _FakeLockConn(self._locks)
        try:
            yield conn
        finally:
            conn.release_all()


class _FakeLockConn:
    def __init__(self, locks: _FakeAdvisoryLocks) -> None:
        self._locks = locks
        self._held: list[asyncio.Lock] = []

    @asynccontextmanager
    async def transaction(self):
        yield None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        if "pg_advisory_xact_lock" not in sql:
            raise AssertionError(f"unexpected SQL on the advisory-lock connection: {sql!r}")
        self._held.append(await self._locks.acquire(tuple(params)))

    def release_all(self) -> None:
        while self._held:
            self._held.pop().release()


@pytest.fixture(autouse=True)
def seed_locks(monkeypatch) -> _FakeAdvisoryLocks:
    """Point the applier's advisory-lock seam at the in-process fake — this suite is
    offline, and the lock is otherwise the one thing in the seed path that opens a
    Postgres of its own. The lock resolves its DSN before it dials, so the host env is
    set too; the fake never connects to it."""
    monkeypatch.setenv("TAI_DATABASE_DEFAULT_PG_HOST", "offline.invalid")
    locks = _FakeAdvisoryLocks()
    monkeypatch.setattr(locks_module, "client_ctx", locks.client_ctx)
    return locks


async def _first_of(*events: asyncio.Event) -> None:
    """Return as soon as ANY of *events* is set."""
    waiters = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for waiter in waiters:
            waiter.cancel()


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


# -- fresh boot: create + LIVE + tool_meta -----------------------------------


def test_fresh_boot_creates_seed_live_and_applies_tool_meta(pg) -> None:
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


# -- idempotence: re-run leaves a present preset untouched --------------------


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

            # No new version was saved across re-runs — the preset is present, so the applier
            # is a pure no-op.
            versions = await instance.app.presets.store.list_versions("echo_default")
            assert len(versions) == 1

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

            # The local-load guard bound the store-active version into THIS worker's
            # registry — the seed is callable on first boot, before any reload.
            assert instance.app.preset_manager.is_registered("echo_default")
            assert "echo_default" in await instance.app.tools.get_tools()
            # No re-ship: the store still holds the sibling's single version.
            versions = await instance.app.presets.store.list_versions("echo_default")
            assert len(versions) == 1

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
            # registration — the store row stays.
            await preset_ops.apply_preset_seeds()
            await instance.app.preset_manager.remove("echo_default")
            assert not instance.app.preset_manager.is_registered("echo_default")
            assert "echo_default" not in await instance.app.tools.get_tools()

            # get_preset succeeds → the present preset ships nothing, and the guard binds the
            # active stored version so the seed is callable.
            await preset_ops._apply_one_seed(seed)
            assert instance.app.preset_manager.is_registered("echo_default")
            assert "echo_default" in await instance.app.tools.get_tools()
            # No re-ship: still the single version.
            versions = await instance.app.presets.store.list_versions("echo_default")
            assert len(versions) == 1

    asyncio.run(run())


# -- concurrent appliers: the version AND the overlay land exactly once -------


def test_concurrent_appliers_keep_version_and_overlay(pg, seed_locks, monkeypatch) -> None:
    """Two workers boot against ONE database and apply the same seed.

    The interleaving driven here is the one that LOSES the overlay when the appliers are
    not serialized: a worker whose presence check missed runs the create core's
    clean-slate overlay cascade AFTER the winner's overlay merge, then hits the create
    conflict, observes the preset present, and applies no overlay of its own — the seed's
    palette metadata is gone for good. Under the per-seed advisory lock that window does
    not exist: the holder creates and overlays as one step, and the second applier waits
    and then observes a complete seed. Both the single version and the full overlay must
    be there at the end.
    """

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

            parked = asyncio.Event()
            first_done = asyncio.Event()
            real_delete_meta = PostgresToolMetaStore.delete_meta

            async def parking_delete_meta(self, tool_name: str) -> None:
                # The second applier holds its create window open at the clean-slate
                # cascade — the very statement that wipes the overlay — until the first
                # applier has FINISHED (the unserialized world) or is proven to be WAITING
                # for the seed lock (the serialized world). Only that task parks; the first
                # applier's own cascade runs straight through.
                task = asyncio.current_task()
                if task is not None and task.get_name() == "applier-second":
                    parked.set()
                    await _first_of(first_done, seed_locks.contended)
                await real_delete_meta(self, tool_name)

            monkeypatch.setattr(PostgresToolMetaStore, "delete_meta", parking_delete_meta)

            second = asyncio.create_task(preset_ops._apply_one_seed(seed), name="applier-second")
            await parked.wait()
            first = asyncio.create_task(preset_ops._apply_one_seed(seed), name="applier-first")
            await first
            first_done.set()
            await second

            # Exactly one preset, one version — no second create, no re-ship.
            records = await instance.app.presets.store.list_presets()
            assert [record.name for record in records] == ["echo_default"]
            assert len(await instance.app.presets.store.list_versions("echo_default")) == 1
            # ...and the overlay survived the race intact (the defect: it is gone).
            meta = await instance.app.tool_meta.store.get_meta("echo_default")
            assert meta is not None
            assert meta.display_name == "Echo Bot"
            assert meta.tags == ["featured"]
            assert meta.folder_id is not None
            # The second applier reached that outcome by WAITING for the seed lock, not by
            # running through the first's window.
            assert seed_locks.contended.is_set()
            # One folder per segment — the two appliers converged on one tree.
            folders = await instance.app.tool_meta.store.list_folders()
            assert sorted(folder.name for folder in folders) == ["acme", "echoes"]
            # Both workers end with the seed live in their own registry.
            assert instance.app.preset_manager.is_registered("echo_default")
            assert "echo_default" in await instance.app.tools.get_tools()

    asyncio.run(run())


# -- an operator's overlay edit survives every later boot --------------------


def test_operator_overlay_edit_survives_reapply(pg) -> None:
    """A seed fills its display metadata only where the overlay leaves it absent, so an
    operator's later rename is still there after the next boot's applier runs — and the
    preset is still on its first version."""

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            seed = PresetSeed(
                name="echo_default",
                description="the shipped echo preset",
                base_tool="echo",
                tool_meta=PresetSeedToolMeta(display_name="Echo Bot", tags=["featured"]),
            )
            instance.app.presets.register_seed(seed)
            await preset_ops.apply_preset_seeds()

            await instance.app.tool_meta.store.merge_meta("echo_default", patch={"display_name": "Operator Label"})

            await preset_ops.apply_preset_seeds()

            meta = await instance.app.tool_meta.store.get_meta("echo_default")
            assert meta is not None
            assert meta.display_name == "Operator Label"
            assert meta.tags == ["featured"]
            assert len(await instance.app.presets.store.list_versions("echo_default")) == 1

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


# -- resolve_folder_path: a lost folder-create race reuses the sibling's folder ---


def test_folder_path_reuses_sibling_folder_on_conflict(pg, monkeypatch) -> None:
    """A multi-worker boot runs many resolvers against ONE Postgres concurrently. With
    ``tool_folders`` carrying ``UNIQUE NULLS NOT DISTINCT (parent_id, name)``, a losing
    worker's create raises :class:`FolderNameConflictError`. The resolver must not raise —
    it re-reads and reuses the winning sibling's folder, so concurrent resolvers converge
    on ONE folder per segment. True fixture concurrency does not interleave (the in-memory
    fake never yields mid-op), so the interleaving is simulated: every segment's create
    loses the race after the sibling commits, mirroring the real UNIQUE-constraint fire."""
    from tai42_skeleton.operations.tool_meta import resolve_folder_path

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            real_create = PostgresToolMetaStore.create_folder
            seen: set[tuple[str | None, str]] = set()
            conflicts = {"n": 0}

            async def racing_create(self, name, parent_id=None):
                # A sibling worker wins this segment's race: it commits the folder first,
                # then THIS worker's INSERT loses on UNIQUE (parent_id, name).
                key = (parent_id, name)
                if key not in seen:
                    seen.add(key)
                    await real_create(self, name, parent_id)
                conflicts["n"] += 1
                raise FolderNameConflictError(name, parent_id)

            monkeypatch.setattr(PostgresToolMetaStore, "create_folder", racing_create)

            # Every segment loses its create race, yet the resolver never raises — it re-reads
            # and reuses the sibling's folder for each segment.
            leaf = await resolve_folder_path("acme/echoes")
            assert conflicts["n"] == 2  # both segments exercised the conflict-reuse branch

            monkeypatch.setattr(PostgresToolMetaStore, "create_folder", real_create)
            folders = {f.id: f for f in await instance.app.tool_meta.store.list_folders()}
            # Exactly one folder per name — the resolvers converged, no duplicate sibling.
            assert sorted(f.name for f in folders.values()) == ["acme", "echoes"]
            assert leaf is not None
            leaf_folder = folders[leaf]
            assert leaf_folder.name == "echoes"
            assert leaf_folder.parent_id is not None
            assert folders[leaf_folder.parent_id].name == "acme"

            # A later resolver on the same fresh path reuses the very same leaf id (convergence).
            assert await resolve_folder_path("acme/echoes") == leaf

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
