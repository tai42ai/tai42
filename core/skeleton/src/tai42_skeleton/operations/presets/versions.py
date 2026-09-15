"""The version-write doors: save a new version, roll back to one, and set a version's tags.

Plus the shared save core that validates before it persists.
"""

from __future__ import annotations

import sys
from typing import Any

from tai42_contract.manifest import ExtensionElement
from tai42_contract.presets import CARRY_FORWARD, CarryForward, PresetBody
from tai42_contract.presets.errors import PresetNotFoundError, PresetVersionNotFoundError
from tai42_contract.states.binding import StateBinding
from tai42_contract.template import TemplatedText
from tai42_contract.versioning.errors import DocumentVersionNotFoundError
from tai42_kit.db import component_store_configured

from tai42_skeleton.app import instance
from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    operation,
)
from tai42_skeleton.operations._broadcast import fleet_fanout
from tai42_skeleton.operations.presets import fanout
from tai42_skeleton.operations.presets.authoring import (
    _attach_body_binding,
    _combo_registry_error,
    _dry_run_bind_error,
    _enforce_registration_tier,
    _input_schema_authoring_error,
    _output_schema_error,
    _write_validator_error,
)
from tai42_skeleton.operations.presets.create import _NOT_CONFIGURED_CODE, _NOT_CONFIGURED_NOUN
from tai42_skeleton.operations.presets.models import PresetRollback, PresetVersionSave, PresetVersionTags
from tai42_skeleton.operations.presets.views import _save_version_response, _wire_snapshot
from tai42_skeleton.operations.response_models_group_a import (
    PresetRollbackResult,
    PresetVersionSaveResult,
    PresetVersionTagsResult,
)

# This submodule's own package generation, captured at import time (a reload builds a
# fresh package + submodules together, so each generation's submodule reads its OWN
# package). ``_agent_authoring_error`` is read through it at call time, so a test's
# ``monkeypatch.setattr`` on the package attribute is honored.
_pkg = sys.modules["tai42_skeleton.operations.presets"]


def _effective_version_body(
    active: PresetBody,
    *,
    fixed_kwargs: dict[str, Any] | None,
    extensions: list[list[ExtensionElement]] | None,
    output_schema: TemplatedText | dict[str, Any] | None,
    output_schema_provided: bool,
    description: str | None,
    input_schema: TemplatedText | dict[str, Any] | CarryForward | None,
) -> PresetBody:
    """The EFFECTIVE new body under the carry-forward sentinels.

    Omitted → carry the active value; an explicit value — including a clearing
    ``[]`` — wins. The store applies the same rule on write; this mirrors it for
    pre-write validation.

    ``input_schema`` carries forward in the STORE (sentinel passed straight through); it
    enters validation only when EXPLICITLY provided (the seed path), so the carry-forward
    sentinel resolves to ``None`` in the effective body. ``description`` is editable per
    version under the None-carry sentinel; the resulting value is validated non-empty in
    the store view (this mirror only feeds the dry run).
    """
    validation_input_schema = None if isinstance(input_schema, CarryForward) else input_schema
    return PresetBody(
        base_tool=active.base_tool,
        description=active.description if description is None else description,
        fixed_kwargs=active.fixed_kwargs if fixed_kwargs is None else fixed_kwargs,
        extensions=active.extensions if extensions is None else extensions,
        output_schema=active.output_schema if not output_schema_provided else output_schema,
        input_schema=validation_input_schema,
    )


async def _validate_version_body(
    name: str,
    body: PresetBody,
    *,
    input_schema: TemplatedText | dict[str, Any] | CarryForward | None,
    fixed_kwargs_provided: bool,
) -> None:
    """Run the SAME validate-before-commit chain create runs over the effective new body.

    A bad edit is a 400 that commits nothing (never a version that can never
    bind, which would brick the preset into delete-only): authoring gate (only
    when ``fixed_kwargs`` was provided) → combo registry → output schema →
    dry-run bake → input-schema support (only when explicitly provided; a
    carried-forward schema was vetted at its own authoring) → write validator.
    Raises the mapped 400.
    """
    # Read the authoring gate through the package object so a monkeypatch bites.
    if fixed_kwargs_provided:
        authoring_error = await _pkg._agent_authoring_error(body.base_tool, body.fixed_kwargs)
        if authoring_error is not None:
            raise BadRequestError(authoring_error)
    combo_error = _combo_registry_error(body.extensions)
    if combo_error is not None:
        raise BadRequestError(combo_error)
    schema_error = await _output_schema_error(body.base_tool, body.output_schema, body.extensions)
    if schema_error is not None:
        raise BadRequestError(schema_error)
    bind_error = await _dry_run_bind_error(
        body.base_tool,
        body.fixed_kwargs,
        name=name,
        description=body.description,
        output_schema=body.output_schema,
        input_schema=body.input_schema,
    )
    if bind_error is not None:
        raise BadRequestError(bind_error)
    if not isinstance(input_schema, CarryForward):
        input_schema_error = _input_schema_authoring_error(body)
        if input_schema_error is not None:
            raise BadRequestError(input_schema_error)
    write_validator_error = await _write_validator_error(body)
    if write_validator_error is not None:
        raise BadRequestError(write_validator_error)


async def _save_version_core(
    name: str,
    *,
    fixed_kwargs: dict[str, Any] | None,
    extensions: list[list[ExtensionElement]] | None,
    output_schema: TemplatedText | dict[str, Any] | None,
    output_schema_provided: bool,
    description: str | None,
    input_schema: TemplatedText | dict[str, Any] | CarryForward | None = CARRY_FORWARD,
    state_binding: StateBinding | CarryForward | None = CARRY_FORWARD,
    tags: list[str] | None = None,
    enforce_tier: bool = True,
) -> tuple[Any, FleetResult]:
    """The save door's reusable save-a-new-version path.

    Read the active body, resolve the carry-forward sentinels, run create's
    validation over the effective new body, (optionally) fence on the base tool's
    authoring tier, save THEN reload (re-pointing the active version back on a
    residual register failure), guard the ``list_changed`` emit on a real
    wire/extension change, and fan the rebind out. Returns the new version row +
    the per-worker fleet report.

    ``input_schema`` carries forward by default (the save door's behavior). ``tags`` labels
    the new version in the SAME save commit (``None`` is the door's untagged save).
    ``enforce_tier`` runs the caller-authorization fence.
    """
    store = instance.app.presets.store
    if instance.app.preset_manager.is_quarantined(name):
        raise ConflictError(f"preset {name!r} is conflicted and is delete-only")

    # Read the active record + body BEFORE any write: the carried-forward base tool
    # + description come from it, the prior active version is captured for the
    # residual-failure re-point, and it gives the 404 for an absent name.
    try:
        prior_record = await store.get_preset(name)
        active = await store.get_active_body(name)
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc

    new_body = _effective_version_body(
        active,
        fixed_kwargs=fixed_kwargs,
        extensions=extensions,
        output_schema=output_schema,
        output_schema_provided=output_schema_provided,
        description=description,
        input_schema=input_schema,
    )
    await _validate_version_body(
        name, new_body, input_schema=input_schema, fixed_kwargs_provided=fixed_kwargs is not None
    )
    # A NEWLY provided binding is attach-validated at SAVE, exactly as create does — its
    # named templates attach idempotently and its expressions/adapters compile, so a bad
    # edit is a 400 that persists nothing. A carried-forward binding was vetted at its
    # own save; an explicit ``null`` clears and attaches nothing.
    await _attach_body_binding(state_binding)
    # Registration-tier fence: the tier is the CURRENT preset's base tool,
    # so editing a fenced base tool's preset is admin-fenced too. Skipped for a platform
    # seed (``enforce_tier=False``) — no caller to fence.
    if enforce_tier:
        await _enforce_registration_tier(new_body.base_tool)

    # Snapshot the OLD wire tool + its extension combos BEFORE the store write —
    # reload tears the old tool down, and after the write the active body already
    # holds the new value.
    old_extensions = active.extensions
    old_wire = await _wire_snapshot(name)
    prior_active = prior_record.active_version
    # Same pre-write point, for the same reason: the save+reload below is the local
    # apply the fan-out's own census would be taken after.
    census = await fanout._census_at_start(fanout._RELOAD_OP)

    try:
        row = await store.save_version(
            name,
            fixed_kwargs=fixed_kwargs,
            extensions=extensions,
            output_schema=output_schema if output_schema_provided else CARRY_FORWARD,
            description=description,
            input_schema=input_schema,
            state_binding=state_binding,
            tags=tags,
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc

    # A residual re-register failure (the environment changed after the pre-write
    # validation) re-points the store's active version back to the prior one so the
    # committed row stays as inert history and store + live never diverge, then
    # re-raises loudly; the emit below is never reached, so a failed save fires
    # nothing.
    try:
        await instance.app.preset_manager.reload(name)
    except Exception:
        await store.rollback(name, prior_active)
        raise

    new_actual_extensions = (await store.get_active_body(name)).extensions
    new_wire = await _wire_snapshot(name)
    if old_wire != new_wire or old_extensions != new_actual_extensions:
        await instance.app.emit_list_changed("tool")
    # The rebind fans out regardless of the emit guard: siblings must re-read the
    # active body even when the wire tool is byte-identical (a baked VALUE changed).
    report = await fanout._fanout_reload(name, census)
    return row, report


@operation(
    summary="Save a new preset version",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotFoundError],
    request_model=PresetVersionSave,
    response_model=PresetVersionSaveResult,
)
async def save_version(
    name: str,
    fixed_kwargs: dict[str, Any] | None,
    extensions: list[list[ExtensionElement]] | None,
    output_schema: TemplatedText | dict[str, Any] | None,
    output_schema_provided: bool,
    description: str | None,
    input_schema: TemplatedText | dict[str, Any] | None = None,
    input_schema_provided: bool = False,
    state_binding: StateBinding | None = None,
    state_binding_provided: bool = False,
) -> dict[str, Any]:
    """Save a new version (carry-forward sentinels on omitted fields) then reload and fan out.

    409 if the record is conflicted, 404 for an absent name. The ``list_changed``
    emit is GUARDED on a real change to the serialized wire tool OR its extension
    combos. The response embeds the per-worker fleet report under ``fanout``.
    """
    # ``input_schema`` mirrors ``output_schema``'s presence flag: an ABSENT field carries
    # the active value forward (the ``CARRY_FORWARD`` sentinel the core accepts), a PRESENT
    # one — including an explicit ``null`` that clears — is the deliberate value the store
    # persists.
    row, report = await _save_version_core(
        name,
        fixed_kwargs=fixed_kwargs,
        extensions=extensions,
        output_schema=output_schema,
        output_schema_provided=output_schema_provided,
        description=description,
        input_schema=input_schema if input_schema_provided else CARRY_FORWARD,
        state_binding=state_binding if state_binding_provided else CARRY_FORWARD,
    )
    return _save_version_response(row, report)


@operation(
    summary="Roll a preset back to a version",
    tags=["presets"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, NotFoundError],
    request_model=PresetRollback,
    response_model=PresetRollbackResult,
)
async def rollback_preset(name: str, version: int) -> dict[str, Any]:
    """Re-point the active version then reload and fan out.

    409 if the record is conflicted, 404 for an absent name or version, 400 if
    the target version cannot bind against the current live registry. The
    response embeds the per-worker fleet report under ``fanout``.
    """
    store = instance.app.presets.store
    if instance.app.preset_manager.is_quarantined(name):
        raise ConflictError(f"preset {name!r} is conflicted and is delete-only")

    try:
        prior_record = await store.get_preset(name)
        old_extensions = (await store.get_active_body(name)).extensions
    except PresetNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} not found") from exc
    old_wire = await _wire_snapshot(name)

    # Rollback has NO carry-forward: read the TARGET version body and validate THAT
    # against the CURRENT live registry (a base tool or extension it named may have
    # been removed since the version was authored), so a rollback to an unbindable
    # version is a 400 that commits nothing rather than a bricking re-point.
    try:
        target = await store.get_version(name, version)
    except PresetVersionNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} has no version {version}") from exc
    target_body = PresetBody.model_validate(target.body)
    combo_error = _combo_registry_error(target_body.extensions)
    if combo_error is not None:
        raise BadRequestError(combo_error)
    schema_error = await _output_schema_error(target_body.base_tool, target_body.output_schema, target_body.extensions)
    if schema_error is not None:
        raise BadRequestError(schema_error)
    bind_error = await _dry_run_bind_error(
        target_body.base_tool,
        target_body.fixed_kwargs,
        name=name,
        description=target_body.description,
        output_schema=target_body.output_schema,
        input_schema=target_body.input_schema,
    )
    if bind_error is not None:
        raise BadRequestError(bind_error)
    write_validator_error = await _write_validator_error(target_body)
    if write_validator_error is not None:
        raise BadRequestError(write_validator_error)
    # Registration-tier fence: the tier is the target body's base tool.
    await _enforce_registration_tier(target_body.base_tool)
    # A rollback ACTIVATES the target version's own door binding, so it is attach-validated
    # here exactly as create and save-version do (templates detached since the version was
    # authored are re-attached idempotently); a binding whose templates are gone is a loud
    # 400 that re-points nothing.
    await _attach_body_binding(target_body.state_binding)

    prior_active = prior_record.active_version
    # Pinned before the rollback+reload local apply — see :func:`_census_at_start`.
    census = await fanout._census_at_start(fanout._RELOAD_OP)
    record = await store.rollback(name, version)

    # Residual re-register failure: re-point the active version back to the prior
    # one so store + live never diverge, then re-raise loudly.
    try:
        await instance.app.preset_manager.reload(name)
    except Exception:
        await store.rollback(name, prior_active)
        raise

    new_extensions = (await store.get_active_body(name)).extensions
    new_wire = await _wire_snapshot(name)
    if old_wire != new_wire or old_extensions != new_extensions:
        await instance.app.emit_list_changed("tool")
    # The rebind fans out regardless of the emit guard: siblings must re-read the
    # active body even when the wire tool is byte-identical (a baked VALUE changed).
    report = await fanout._fanout_reload(name, census)
    # Embed the per-worker rebind fan-out report (mirrors the template writers): the
    # read-your-writes barrier proving the rolled-back version reached every worker.
    return {"name": name, "active_version": record.active_version, "fanout": fleet_fanout(report)}


@operation(
    summary="Set a preset version's tags",
    tags=["presets"],
    destructive=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
    request_model=PresetVersionTags,
    response_model=PresetVersionTagsResult,
)
async def set_preset_version_tags(name: str, version: str, tags: list[str]) -> dict[str, Any]:
    """Replace one version's ``tags`` annotation.

    Tags are labels on an immutable version body, so this edits only the
    annotation and never rebinds the live tool (no reload / no fan-out). 404 for
    an unknown preset or version; a 501 ``NotSupportedError``
    (versioning-not-configured) on a store-less deploy, exactly as the create
    route refuses.
    """
    try:
        version_num = int(version)
    except ValueError as exc:
        raise BadRequestError("version must be an integer") from exc

    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_NOT_CONFIGURED_NOUN), extra={"code": _NOT_CONFIGURED_CODE})

    try:
        await instance.app.presets.set_version_tags(name, version_num, tags)
    except DocumentVersionNotFoundError as exc:
        raise NotFoundError(f"preset {name!r} has no version {version_num}") from exc
    return {"name": name, "version": version_num, "tags": tags}
