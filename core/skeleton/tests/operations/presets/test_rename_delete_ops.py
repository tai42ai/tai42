"""Oracles for the name-lifecycle doors: rename typed-store / compensate branches
and the conflicted-record hard-delete failure path."""

from __future__ import annotations

import asyncio

import pytest
from tai42_contract.presets import PresetBody
from tai42_contract.presets.errors import PresetExistsError, PresetNameConflictError

from tai42_skeleton.app import instance
from tai42_skeleton.operations import BadRequestError, ConflictError
from tai42_skeleton.operations import presets as preset_ops

from .conftest import _create, _manifest

# -- rename: new-name validity precedes every existence/state check ----------


def test_rename_invalid_new_name_precedes_not_found_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A MISSING preset with an INVALID new name is a 400 "invalid preset name",
            # not a 404: new-name tool-name safety is validated FIRST, before the
            # existence check — name validity precedes not-found.
            with pytest.raises(BadRequestError, match="invalid preset name"):
                await preset_ops.rename_preset(name="missing", new_name="a/b")

    asyncio.run(run())


# -- rename: typed store errors + compensate branches ------------------------


def test_rename_store_exists_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def boom(self, a, b):
                raise PresetExistsError(b)

            monkeypatch.setattr(type(instance.app.presets.store), "rename_preset", boom)
            with pytest.raises(ConflictError, match="already exists"):
                await preset_ops.rename_preset(name="old", new_name="new")

    asyncio.run(run())


def test_rename_store_name_conflict_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def boom(self, a, b):
                raise PresetNameConflictError(b)

            monkeypatch.setattr(type(instance.app.presets.store), "rename_preset", boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await preset_ops.rename_preset(name="old", new_name="new")

    asyncio.run(run())


def test_rename_compensates_and_maps_exists_on_reload_failure(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def reload_boom(name):
                raise PresetExistsError(name)

            monkeypatch.setattr(instance.app.preset_manager, "reload", reload_boom)
            with pytest.raises(ConflictError, match="already exists"):
                await preset_ops.rename_preset(name="old", new_name="new")
            # The store move was compensated: the preset stays live under its old name.
            assert (await instance.app.presets.store.get_preset("old")).name == "old"

    asyncio.run(run())


def test_rename_compensates_and_maps_name_conflict_on_reload_failure(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("old", fixed_kwargs={"units": "v"})

            async def reload_boom(name):
                raise PresetNameConflictError(name)

            monkeypatch.setattr(instance.app.preset_manager, "reload", reload_boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await preset_ops.rename_preset(name="old", new_name="new")

    asyncio.run(run())


# -- delete: conflicted hard-delete failure ----------------------------------


def test_delete_conflicted_hard_delete_failure_reraises(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A stored preset whose NAME is a live tool rehydrates into quarantine.
            body = PresetBody(base_tool="echo", description="d", fixed_kwargs={}, extensions=[])
            await instance.app.versioning.store.create("preset", "weather", body.model_dump())
            await instance.app.preset_manager.rehydrate()
            assert instance.app.preset_manager.is_quarantined("weather")

            async def del_boom(self, *a, **k):
                raise RuntimeError("hard delete kaput")

            monkeypatch.setattr(type(instance.app.versioning.store), "delete", del_boom)
            with pytest.raises(RuntimeError, match="hard delete kaput"):
                await preset_ops.delete_preset(name="weather")

    asyncio.run(run())
