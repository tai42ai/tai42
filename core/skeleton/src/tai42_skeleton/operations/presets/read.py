"""The preset read doors: list, get one, list/get a version, and the referees preview."""

from __future__ import annotations

from typing import Any

from tai42_contract.presets.errors import PresetNotFoundError, PresetVersionNotFoundError
from tai42_contract.versioning.models import DocumentVersion
from tai42_kit.db import component_store_configured

from tai42_skeleton.app import instance
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations.presets.references import _reference_maps, _rename_referees
from tai42_skeleton.operations.presets.views import _store_record_view
from tai42_skeleton.operations.response_models_group_a import (
    DocumentVersionList,
    PresetDetailView,
    PresetRecordList,
    PresetRefereesResult,
)


@operation(summary="List presets", tags=["presets"], response_model=PresetRecordList)
async def list_presets() -> list[dict[str, Any]]:
    """One row per store-backed record (the presets plus the ``conflicted``
    quarantined ones) — the population the presets management table shows."""
    rows: list[dict[str, Any]] = []
    # A store-less deploy (no versioned store configured) has no presets — skip the
    # Postgres read and serve an empty list.
    if component_store_configured(SKELETON_COMPONENT):
        records = await instance.app.presets.store.list_presets()
        # One batched active-body read instead of a per-record round-trip (N+1).
        bodies = await instance.app.presets.list_active_bodies()
        # Both cross-reference maps in one pass over the population.
        uses_map, used_by_map = _reference_maps(bodies)
        # ``records`` and ``bodies`` are two separate reads; a preset deleted between
        # them is gone from ``bodies`` — skip it rather than KeyError, it is no longer
        # a live row to list.
        rows = [
            _store_record_view(
                rec.name,
                rec.active_version,
                bodies[rec.name],
                uses=uses_map[rec.name],
                used_by=used_by_map[rec.name],
            )
            for rec in records
            if rec.name in bodies
        ]
    return rows


@operation(summary="Get a preset", tags=["presets"], errors=[NotFoundError], response_model=PresetDetailView)
async def get_preset(name: str) -> dict[str, Any]:
    """The store record + the active ``fixed_kwargs`` + the ``uses`` / ``used_by``
    cross-references; 404 for an absent name."""
    try:
        record = await instance.app.presets.store.get_preset(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    # Read the active-body population so this row's references compute identically to
    # the list route. A name gone between the record read and here was deleted
    # concurrently — a genuine 404, never a silent KeyError.
    bodies = await instance.app.presets.list_active_bodies()
    if name not in bodies:
        raise NotFoundError(f"preset {name!r} not found")
    body = bodies[name]
    uses_map, used_by_map = _reference_maps(bodies)
    view = _store_record_view(name, record.active_version, body, uses=uses_map[name], used_by=used_by_map[name])
    view["fixed_kwargs"] = body.fixed_kwargs
    return view


@operation(
    summary="List a preset's versions",
    tags=["presets"],
    errors=[NotFoundError],
    response_model=DocumentVersionList,
)
async def list_versions(name: str) -> list[dict[str, Any]]:
    """The full version history for a preset; 404 for an absent name."""
    try:
        versions = await instance.app.presets.store.list_versions(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    return [v.model_dump() for v in versions]


@operation(
    summary="Get a specific preset version",
    tags=["presets"],
    errors=[BadRequestError, NotFoundError],
    response_model=DocumentVersion,
)
async def get_version(name: str, version: str) -> dict[str, Any]:
    """One version of a preset by its integer version number; a non-integer segment
    is a 400 and an unknown version a 404."""
    try:
        version_num = int(version)
    except ValueError as exc:
        raise BadRequestError("version must be an integer") from exc
    try:
        row = await instance.app.presets.store.get_version(name, version_num)
    except PresetVersionNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} has no version {version_num}") from exc
    return row.model_dump()


@operation(
    summary="List live references to this preset",
    tags=["presets"],
    errors=[NotFoundError],
    response_model=PresetRefereesResult,
)
async def preset_referees(name: str) -> dict[str, Any]:
    """Every live reference a rename of this preset would strand — the SAME full union
    the rename door blocks on: the OTHER presets whose active body composes it, plus every
    registered referee (platform wiring — schedules/hooks/routes/extensions/parks — and
    plugin holders). Exposed so the UI can preflight a rename. 404 for an unknown preset,
    the same existence check the rename door runs first; a referee raising propagates
    loudly, exactly as at the rename gate."""
    # A store-less deploy holds no preset, so an unknown name is a genuine 404
    # without a Postgres open (the rename/delete doors' reasoning).
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"preset {name!r} not found")
    try:
        await instance.app.presets.store.get_preset(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    referees = await _rename_referees(name)
    return {"name": name, "referees": referees}
