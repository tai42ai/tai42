"""Concurrent preset creates against a REAL Postgres.

Every process of a deployment (each ``tai serve`` worker and the backend worker) runs
the declared-preset-seed applier at boot against ONE database. Unserialized, an applier
whose presence check missed still runs the create core's clean-slate overlay cascade,
which can land AFTER the winner's overlay merge — the seed keeps its version but loses
its tool_meta overlay, and the loser then observes the preset present and applies no
overlay of its own. The per-seed advisory lock closes that window: this drives the exact
interleaving over real Postgres row locks and asserts both the single version and the
full overlay are there afterwards, and that an operator's later edit survives the next
boot's applier.

The interleaving is chosen, not hoped for: the second applier holds its create window
open at the clean-slate cascade until the first applier has finished or has entered the
name-lock acquire, so the test is deterministic in both worlds and hangs in neither.

The create door reaches that same lock, so a create of a name a sibling already claimed
conflicts on the stored row before the cascade — the second test drives it over the real
stores.

OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a
live Postgres. Without the opt-in it SKIPS VISIBLY with a clear reason.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import LiteralString, NamedTuple

import pytest
from tai42_contract.presets import PresetSeed, PresetSeedToolMeta
from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

import tai42_skeleton.db.locks as db_locks
import tai42_skeleton.tool_meta.store as tool_meta_store
import tai42_skeleton.versioning.store as versioning_store
from tai42_skeleton.app import instance
from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry
from tai42_skeleton.manifest import Manifest
from tai42_skeleton.operations import ConflictError
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.tool_meta.store import PostgresToolMetaStore

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"

_MANIFEST = {
    "extensions_modules": ["tests.presets._ext_fixtures"],
    "tools": [{"title": "fx", "module": "tests.presets._fixtures", "include": ["weather", "echo"]}],
}


def _manifest() -> Manifest:
    return Manifest.model_validate(_MANIFEST)


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@pytest.fixture
async def real_seed(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[PresetSeed]:
    """A uniquely named seed over a real Postgres, cleaned up afterwards.

    The suite-wide autouse fixtures point the versioned-document store, the tool_meta
    store and the advisory lock at in-memory fakes so the offline suite never opens
    Postgres; these tests need the real ones (only real row locks and a real advisory lock
    can exhibit the race), so restore the genuine seams.
    """
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres seed-concurrency test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs a real advisory lock — no fake)"
        )
    monkeypatch.setattr(versioning_store, "client_ctx", client_ctx)
    monkeypatch.setattr(tool_meta_store, "client_ctx", client_ctx)
    monkeypatch.setattr(db_locks, "client_ctx", client_ctx)
    # Rebuild the cached settings so ``TAI_DATABASE_DEFAULT_PG_*`` from the environment is read.
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    name = f"seed_race_it_{uuid.uuid4().hex}"
    seed = PresetSeed(
        name=name,
        description="the shipped echo preset",
        base_tool="echo",
        fixed_kwargs={"text": "hello"},
        # A folder path unique to this run, so the cleanup below owns every row it drops.
        tool_meta=PresetSeedToolMeta(display_name="Echo Bot", tags=["featured"], folder_path=name),
    )
    try:
        yield seed
    finally:
        mgr = instance.app.preset_manager
        if mgr.is_registered(name):
            await mgr.remove(name)
        await _exec("DELETE FROM tool_meta WHERE tool_name = %s", (name,))
        await _exec("DELETE FROM versioned_documents WHERE kind = 'preset' AND name = %s", (name,))
        await _exec("DELETE FROM tool_folders WHERE name = %s", (name,))


class _RaceGates(NamedTuple):
    """``parked`` fires when the second applier is holding its create window open;
    setting ``released`` lets it proceed."""

    parked: asyncio.Event
    released: asyncio.Event


@pytest.fixture
def race_gates(monkeypatch: pytest.MonkeyPatch) -> Iterator[_RaceGates]:
    """Park the ``applier-second`` task at the create core's clean-slate cascade — the
    very statement that wipes the overlay — until the ``applier-first`` task has entered
    the name-lock acquire (the serialized world) or the test releases it because the first
    applier finished (the unserialized world). ``parked`` is what the test waits on before
    it starts the first applier, which fixes the order: second checks, first creates."""
    parked = asyncio.Event()
    released = asyncio.Event()
    real_delete_meta = PostgresToolMetaStore.delete_meta
    real_lock = preset_ops.advisory_name_lock

    def _is(task_name: str) -> bool:
        task = asyncio.current_task()
        return task is not None and task.get_name() == task_name

    async def parking_delete_meta(self, tool_name: str) -> None:
        if _is("applier-second"):
            parked.set()
            await released.wait()
        await real_delete_meta(self, tool_name)

    def signalling_lock(namespace: int, name: str):
        # The first applier is about to WAIT for the lock the second holds: releasing the
        # second here is what lets the serialized world make progress.
        if _is("applier-first"):
            released.set()
        return real_lock(namespace, name)

    monkeypatch.setattr(PostgresToolMetaStore, "delete_meta", parking_delete_meta)
    monkeypatch.setattr(preset_ops, "advisory_name_lock", signalling_lock)
    yield _RaceGates(parked, released)
    released.set()


async def test_concurrent_appliers_keep_version_and_overlay_on_real_postgres(
    real_seed: PresetSeed, race_gates: _RaceGates
) -> None:
    """Two appliers race one seed over real Postgres: exactly one version, and the seed's
    tool_meta overlay (display name, tags, resolved folder) intact — the palette metadata
    the unserialized applier loses."""
    async with instance.app.app_context(_manifest()):
        second = asyncio.create_task(preset_ops._apply_one_seed(real_seed), name="applier-second")
        await race_gates.parked.wait()
        first = asyncio.create_task(preset_ops._apply_one_seed(real_seed), name="applier-first")
        await first
        # The winner is done: an applier that was never serialized behind it may proceed.
        race_gates.released.set()
        await second

        store = instance.app.presets.store
        record = await store.get_preset(real_seed.name)
        assert record.name == real_seed.name
        assert len(await store.list_versions(real_seed.name)) == 1

        meta = await instance.app.tool_meta.store.get_meta(real_seed.name)
        assert meta is not None
        assert meta.display_name == "Echo Bot"
        assert meta.tags == ["featured"]
        assert meta.folder_id is not None
        folders = [f for f in await instance.app.tool_meta.store.list_folders() if f.name == real_seed.name]
        assert [f.id for f in folders] == [meta.folder_id]

        # The seed is live in this process, and an operator's later rename survives the
        # next boot's applier — the overlay is filled only where it is absent.
        assert instance.app.preset_manager.is_registered(real_seed.name)
        await instance.app.tool_meta.store.merge_meta(real_seed.name, patch={"display_name": "Operator Label"})
        await preset_ops._apply_one_seed(real_seed)
        after = await instance.app.tool_meta.store.get_meta(real_seed.name)
        assert after is not None
        assert after.display_name == "Operator Label"
        assert after.tags == ["featured"]
        assert len(await store.list_versions(real_seed.name)) == 1


async def test_create_door_on_a_present_name_keeps_its_overlay_on_real_postgres(real_seed: PresetSeed) -> None:
    """The create door on a name a sibling worker already created: the claim conflicts on
    the stored row before the clean-slate cascade, so the preset keeps its overlay."""
    async with instance.app.app_context(_manifest()):
        await preset_ops._apply_one_seed(real_seed)
        # Model the sibling's create: the store row and its overlay stay, this worker's
        # registration (what the door's local pre-checks read) does not.
        await instance.app.preset_manager.remove(real_seed.name)

        with pytest.raises(ConflictError, match="already exists"):
            await preset_ops.create_preset(
                name=real_seed.name,
                base_tool="echo",
                description="an operator create of a name a sibling already claimed",
                fixed_kwargs={"text": "hello"},
                extensions=[],
                output_schema=None,
            )

        meta = await instance.app.tool_meta.store.get_meta(real_seed.name)
        assert meta is not None
        assert meta.display_name == "Echo Bot"
        assert meta.tags == ["featured"]
        assert meta.folder_id is not None
        assert len(await instance.app.presets.store.list_versions(real_seed.name)) == 1
