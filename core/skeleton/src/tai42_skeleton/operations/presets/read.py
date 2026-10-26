"""The preset read doors: list, get one, list/get a version, and the referees preview."""

from __future__ import annotations

import logging
from typing import Any, Literal

from tai42_contract.presets.errors import PresetNotFoundError, PresetVersionNotFoundError
from tai42_contract.versioning.models import DocumentVersion
from tai42_kit.db import component_store_configured

from tai42_skeleton.app import instance
from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.operations import BadRequestError, NotFoundError, ValidationRejectedError, operation
from tai42_skeleton.operations.errors import OperationFailedError
from tai42_skeleton.operations.holders import bound_branches, delete_holders, holders_preview, rename_holders
from tai42_skeleton.operations.presets.models import PresetRefereesQuery
from tai42_skeleton.operations.presets.views import _store_record_view
from tai42_skeleton.operations.reference_graph import StoredPresets, _reference_maps
from tai42_skeleton.operations.response_models.holders import HoldersPreview
from tai42_skeleton.operations.response_models.presets import PresetDetailView, PresetRecordList
from tai42_skeleton.operations.response_models.shared import DocumentVersionList
from tai42_skeleton.presets.secret_refs import mask_all_baked_secrets, redact_baked_secrets
from tai42_skeleton.tools.binding.errors import UnknownToolError

logger = logging.getLogger(__name__)


async def _redacted_fixed_kwargs(preset_name: str, base_tool: str, fixed_kwargs: dict[str, Any]) -> dict[str, Any]:
    """Mask every baked value under a secret-typed leaf of ``base_tool``'s input schema.

    The one read-view redaction seam every preset read door funnels through, so no two
    doors drift: :func:`get_preset`, :func:`get_version` and :func:`list_versions` all
    call it. A baked ``SecretStr`` / ``format: password`` / ``writeOnly`` leaf is masked
    while an ``!ENV`` marker is kept (see :func:`redact_baked_secrets`); the stored body
    and the bind / execute paths keep the real value. When ``base_tool`` is not
    registered — an unbindable preset whose plugin is absent — no schema tells a secret
    leaf from a config one, so the read fails CLOSED and masks every baked leaf, logging
    a warning naming the preset and the unresolved base tool so the degraded read view is
    visible rather than a silent blanket mask (names only, never a baked value).
    """
    try:
        base = await instance.app.tools.get_tool(base_tool)
    except UnknownToolError:
        logger.warning(
            "preset %r read masks every baked value: its base tool %r is not registered, "
            "so secret-typed kwargs cannot be told from plain config",
            preset_name,
            base_tool,
        )
        return mask_all_baked_secrets(fixed_kwargs)
    return redact_baked_secrets(fixed_kwargs, base.parameters)


async def _redact_version_body(preset_name: str, body: dict[str, Any]) -> dict[str, Any]:
    """Return a version ``body`` with its baked ``fixed_kwargs`` masked through the read seam.

    A version body is an opaque store dict; only a preset-kind body carries both a
    ``base_tool`` and a ``fixed_kwargs`` mapping — redact it in place then, else pass the
    body through unchanged.
    """
    base_tool = body.get("base_tool")
    fixed_kwargs = body.get("fixed_kwargs")
    if isinstance(base_tool, str) and isinstance(fixed_kwargs, dict):
        body["fixed_kwargs"] = await _redacted_fixed_kwargs(preset_name, base_tool, fixed_kwargs)
    return body


@operation(summary="List presets", tags=["presets"], errors=[OperationFailedError], response_model=PresetRecordList)
async def list_presets() -> list[dict[str, Any]]:
    """One row per store-backed record (the presets plus the ``conflicted`` quarantined ones).

    The population the presets management table shows.
    """
    # One batched read of the population (its store gate answers no presets on a
    # deployment with no skeleton database); rows come in name order.
    versioned = await instance.app.presets.list_active_versioned_bodies()
    bodies = {name: body for name, (_version, body) in versioned.items()}
    # Both cross-reference maps in one pass over the population.
    uses_map, used_by_map = await _reference_maps(bodies)
    return [
        _store_record_view(name, version, body, uses=uses_map[name], used_by=used_by_map[name])
        for name, (version, body) in versioned.items()
    ]


@operation(
    summary="Get a preset",
    tags=["presets"],
    errors=[NotFoundError, OperationFailedError],
    response_model=PresetDetailView,
)
async def get_preset(name: str) -> dict[str, Any]:
    """The store record + the active ``fixed_kwargs`` + the ``uses`` / ``used_by`` cross-references.

    404 for an absent name.
    """
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
    uses_map, used_by_map = await _reference_maps(bodies)
    view = _store_record_view(name, record.active_version, body, uses=uses_map[name], used_by=used_by_map[name])
    view["fixed_kwargs"] = await _redacted_fixed_kwargs(name, body.base_tool, body.fixed_kwargs)
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
    dumped: list[dict[str, Any]] = []
    for version in versions:
        row = version.model_dump()
        row["body"] = await _redact_version_body(name, row["body"])
        dumped.append(row)
    return dumped


@operation(
    summary="Get a specific preset version",
    tags=["presets"],
    errors=[BadRequestError, NotFoundError],
    response_model=DocumentVersion,
)
async def get_version(name: str, version: str) -> dict[str, Any]:
    """One version of a preset by its integer version number.

    A non-integer segment is a 400 and an unknown version a 404.
    """
    try:
        version_num = int(version)
    except ValueError as exc:
        raise BadRequestError("version must be an integer") from exc
    try:
        row = await instance.app.presets.store.get_version(name, version_num)
    except PresetVersionNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} has no version {version_num}") from exc
    result = row.model_dump()
    result["body"] = await _redact_version_body(name, result["body"])
    return result


async def require_preset(name: str) -> None:
    """Raise the 404 for a name that is neither a quarantined record nor a stored preset.

    A store-less deploy holds no preset, so an unquarantined name is a genuine 404 without a
    Postgres open.
    """
    if instance.app.preset_manager.is_quarantined(name):
        return
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotFoundError(f"preset {name!r} not found")
    try:
        await instance.app.presets.store.get_preset(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc


@operation(
    summary="List live holders of this preset",
    tags=["presets"],
    errors=[NotFoundError, OperationFailedError, ValidationRejectedError],
    request_model=PresetRefereesQuery,
    response_model=HoldersPreview,
)
async def preset_referees(name: str, action: Literal["rename", "delete"]) -> dict[str, Any]:
    """Every live holder a rename or delete of this preset would be refused on — the gate's own gather.

    ``action`` picks the gather: ``rename`` answers what the rename door blocks on, ``delete``
    what the delete door blocks on, each over the preset and every extension branch tool bound
    off it. 404 for an unknown preset; a referee raising propagates loudly, exactly as at the gate.
    """
    if action not in ("rename", "delete"):
        raise ValidationRejectedError(f"action is 'rename' or 'delete', got {action!r}")
    await require_preset(name)
    branches = bound_branches(name)
    if action == "rename":
        holders = await rename_holders(name, branches, await StoredPresets.read())
    else:
        holders = await delete_holders(name, branches)
    return await holders_preview(action, holders)
