"""Oracles for the create door: the destructive projection hint, the typed store /
residual register-failure branches, the create response references, and the write validator."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tai42_contract.manifest import ApiToolsConfig
from tai42_contract.presets.errors import (
    PresetExistsError,
    PresetNameConflictError,
    PresetNotFoundError,
)

from tai42_skeleton.app import instance
from tai42_skeleton.operations import ConflictError, OperationRegistry, operation_metadata_of
from tai42_skeleton.operations import presets as preset_ops
from tai42_skeleton.operations.projection import project_operations

# Importing the router registers the routes, which forces ``destructive`` on the
# DELETE op (the adapter's DELETE rule) so the projection oracle sees it.
from tai42_skeleton.routers import presets as _presets_router  # noqa: F401

from .conftest import _create, _manifest

# -- destructive projection --------------------------------------------------


def test_destructive_preset_ops_project_with_destructive_hint() -> None:
    """The mutating preset ops carry ``destructiveHint`` when projected; the reads
    and the dry-run validate do not. ``delete_preset`` gets its destructive flag from
    the adapter's DELETE rule (the router import above forced it)."""
    all_ops = (
        "list_presets",
        "create_preset",
        "get_preset",
        "list_versions",
        "get_version",
        "save_version",
        "rollback_preset",
        "rename_preset",
        "delete_preset",
        "preset_referees",
        "validate_preset",
        "set_preset_version_tags",
    )
    destructive = {
        "create_preset",
        "save_version",
        "rollback_preset",
        "rename_preset",
        "delete_preset",
        "set_preset_version_tags",
    }

    reg = OperationRegistry()
    for name in all_ops:
        op = operation_metadata_of(getattr(preset_ops, name))
        assert op.destructive is (op.name in destructive), f"{op.name} destructive={op.destructive}"
        reg.register(op)

    class _Rec:
        def __init__(self) -> None:
            self.registered: dict[str, Any] = {}

        def tool(self, *, force, name, tags, annotations):
            self.registered[name] = annotations
            return lambda fn: fn

    class _App:
        def __init__(self) -> None:
            self.tools = _Rec()

    app = _App()
    project_operations(app, ApiToolsConfig(expose_destructive=True), registry=reg)
    for name in destructive:
        annotations = app.tools.registered[name]
        assert annotations is not None
        assert annotations.destructiveHint is True
    for name in ("list_presets", "get_preset", "validate_preset"):
        assert app.tools.registered[name] is None


# -- create: typed store errors + residual register-failure branches ---------


def test_create_store_name_conflict_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def boom(self, *a, **k):
                raise PresetNameConflictError("weather")

            # The store view is rebuilt per access, so the seam is patched on the class.
            monkeypatch.setattr(type(instance.app.presets.store), "create_preset", boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await _create("p", fixed_kwargs={"units": "v"})

    asyncio.run(run())


def test_create_store_exists_maps_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def boom(self, *a, **k):
                raise PresetExistsError("p")

            monkeypatch.setattr(type(instance.app.presets.store), "create_preset", boom)
            with pytest.raises(ConflictError, match="already exists"):
                await _create("p", fixed_kwargs={"units": "v"})

    asyncio.run(run())


def test_create_of_a_store_present_name_keeps_its_overlay_and_409s(pg) -> None:
    """The create door on a name that is already a live preset in the STORE but not in
    THIS worker's registry — a sibling worker's create or a boot seed applier, neither of
    which fans out here. The locked claim conflicts on the stored row BEFORE the
    clean-slate overlay cascade, so the existing preset keeps its display metadata."""

    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            await _create("p", fixed_kwargs={"units": "v"})
            await instance.app.tool_meta.store.merge_meta("p", patch={"display_name": "Kept"})
            # Model the sibling's create: the store row stays, this worker's registration
            # (what the door's local pre-checks read) does not.
            await instance.app.preset_manager.remove("p")

            with pytest.raises(ConflictError, match="already exists"):
                await _create("p", fixed_kwargs={"units": "v"})

            meta = await instance.app.tool_meta.store.get_meta("p")
            assert meta is not None
            assert meta.display_name == "Kept"

    asyncio.run(run())


def test_create_register_exists_race_rolls_back_and_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def boom(*a, **k):
                raise PresetExistsError("p")

            monkeypatch.setattr(instance.app.preset_manager, "register", boom)
            with pytest.raises(ConflictError, match="already exists"):
                await _create("p", fixed_kwargs={"units": "v"})
            # The store row was rolled back (HARD delete) — no stored-but-unregistered
            # preset survives.
            with pytest.raises(PresetNotFoundError):
                await instance.app.presets.store.get_preset("p")

    asyncio.run(run())


def test_create_register_name_conflict_race_rolls_back_and_409(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def boom(*a, **k):
                raise PresetNameConflictError("p")

            monkeypatch.setattr(instance.app.preset_manager, "register", boom)
            with pytest.raises(ConflictError, match="collides with an existing tool"):
                await _create("p", fixed_kwargs={"units": "v"})

    asyncio.run(run())


def test_create_rollback_delete_failure_reraises_delete_error(pg, monkeypatch) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def reg_boom(*a, **k):
                raise RuntimeError("register kaput")

            async def del_boom(*a, **k):
                raise RuntimeError("delete kaput")

            monkeypatch.setattr(instance.app.preset_manager, "register", reg_boom)
            monkeypatch.setattr(type(instance.app.versioning.store), "delete", del_boom)
            # The delete failure during rollback surfaces loudly (chained from the
            # register failure), never swallowed.
            with pytest.raises(RuntimeError, match="delete kaput"):
                await _create("p", fixed_kwargs={"units": "v"})

    asyncio.run(run())


# -- create response cross-references ----------------------------------------


def test_create_returns_uses_and_updates_used_by(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # A live preset the composing create can name as a tool.
            await _create("leaf", base_tool="weather", fixed_kwargs={"units": "v"})
            created = await preset_ops.create_preset(
                name="composer",
                base_tool="authorable_agent",
                description="d",
                fixed_kwargs={"tool_names": ["leaf"]},
                extensions=[],
                output_schema=None,
            )
            assert created["uses"] == ["leaf"]
            assert created["used_by"] == []
            rows = {r["name"]: r for r in await preset_ops.list_presets()}
            assert rows["leaf"]["used_by"] == ["composer"]

    asyncio.run(run())


def test_plain_create_returns_empty_references(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            created = await preset_ops.create_preset(
                name="plain",
                base_tool="weather",
                description="d",
                fixed_kwargs={"units": "v"},
                extensions=[],
                output_schema=None,
            )
            assert created["uses"] == []
            assert created["used_by"] == []

    asyncio.run(run())


# -- per-base-tool write validator (create leg) ------------------------------

# A base-tool plugin registers a write validator for its base tool; the preset
# write path consults it on the full body before persisting. The registry is reset
# each ``start()``, so each ``app_context`` opens with a clean one and a test
# registers a validator for the fixture ``weather`` base tool.


def test_create_rejected_by_write_validator_400(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body):
                return ["weather rejects this", "second reason"]

            instance.app.presets.register_write_validator("weather", validator)
            with pytest.raises(preset_ops.BadRequestError) as exc_info:
                await _create("w", fixed_kwargs={"units": "v"})
            # Issues verbatim, one per line, and no row persisted.
            assert exc_info.value.message == "weather rejects this\nsecond reason"
            with pytest.raises(PresetNotFoundError):
                await instance.app.presets.store.get_preset("w")

    asyncio.run(run())


def test_create_accepted_when_write_validator_passes(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body):
                return []

            instance.app.presets.register_write_validator("weather", validator)
            await _create("w", fixed_kwargs={"units": "v"})
            assert (await instance.app.presets.store.get_preset("w")).name == "w"

    asyncio.run(run())


def test_create_untouched_when_no_write_validator(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):
            # No validator registered for the base tool — the write path runs ungated.
            await _create("w", fixed_kwargs={"units": "v"})
            assert (await instance.app.presets.store.get_preset("w")).name == "w"

    asyncio.run(run())


def test_create_write_validator_exception_propagates(pg) -> None:
    async def run() -> None:
        async with instance.app.app_context(_manifest()):

            async def validator(body):
                raise RuntimeError("validator kaput")

            instance.app.presets.register_write_validator("weather", validator)
            # A validator exception surfaces loudly — never swallowed, never a pass.
            with pytest.raises(RuntimeError, match="validator kaput"):
                await _create("w", fixed_kwargs={"units": "v"})

    asyncio.run(run())
