"""Deployment-config operations — ``/api/config/*``.

A thin skin over the config facets (``tai42_app.config``), the admin reload seam
(``tai42_app.admin``), the settings registry, and the worker bus:

* ``read_env`` — the stored env map plus the operator's secret-key marks.
* ``write_env`` — merge a ``{key: value}`` env map (all values strings) through the
  :class:`~tai42_skeleton.config.service.ConfigService` pipeline: validate the effective
  config, write the env, hot-reload the process, and broadcast the reload to the fleet.
* ``read_mode`` — the active config backend mode (``file`` / ``k8s``).
* ``read_settings_schema`` — every registered settings group with per-field current
  resolved values.
* ``reload_config`` — a soft-restart (refresh env, reset settings caches,
  re-initialize from the manifest) on this worker, broadcast to the fleet. Distinct
  from the fleet soft-restart door ``fleet_reload_config`` (``/api/fleet/reload-config``).

``write_env`` and ``reload_config`` mutate the running deployment, so both are
``destructive`` and honor the reload gate.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import BaseModel, RootModel
from starlette.background import BackgroundTask
from tai42_contract.app import tai42_app
from tai42_contract.settings_profiles import SettingsProfileBody
from tai42_contract.settings_profiles.errors import (
    SettingsProfileExistsError,
    SettingsProfileNotFoundError,
    SettingsProfileVersionNotFoundError,
)
from tai42_kit.db import component_store_configured
from tai42_kit.settings import registered_settings

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.app.epoch import _reload_driven_by_request
from tai42_skeleton.app.graceful_exit import request_serve_graceful_exit
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config import config_mode
from tai42_skeleton.config.boundary import reload_class_by_env_var
from tai42_skeleton.config.recycle_policy import capability_report
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.operations import BadRequestError, NotFoundError, NotSupportedError, OperationResponse, operation
from tai42_skeleton.operations._broadcast import apply_response, broadcast, profile_apply_response

# Importing this module registers ``EnvSecretMarksSettings`` (registration runs at
# class-definition time) so the marks group appears in the settings schema, and
# exposes the reload-aware accessor used by the env read.
from tai42_skeleton.settings.env_secret_marks import env_secret_marks_settings
from tai42_skeleton.settings_profiles.store import SettingsProfileStoreView, settings_profile_store


class EnvUpdate(RootModel[dict[str, str]]):
    """An env override map — a ``{name: value}`` object whose values are all
    strings, merged into the stored env config before a hot reload."""


class ReloadConfigRequest(BaseModel):
    """A local reload-config request — an optional ``targets`` list restricting the
    fan-out of the soft-restart to named workers (all workers when omitted)."""

    targets: list[str] | None = None


def _stored_env() -> dict[str, str]:
    """The stored env map, treating a missing ``.env`` as an empty map.

    A never-written env store (``FileNotFoundError``) means "no stored overrides",
    not a failure — the caller falls back to process env / defaults.
    """
    try:
        return tai42_app.config.config_manager.read_env()
    except FileNotFoundError:
        return {}


@operation(summary="Read the stored env config and secret-key marks", tags=["config"])
async def read_env() -> dict:
    """Return the stored env map alongside the operator's secret-key marks.

    ``data.env`` is the stored env key-value map (a never-written store yields
    an empty map). ``data.secret_keys`` is the current
    ``EnvSecretMarksSettings.secret_keys`` — the env key names the operator
    marked secret so the editor masks them on display.
    """
    return {"env": _stored_env(), "secret_keys": env_secret_marks_settings().secret_keys}


@operation(summary="Read the active config backend mode", tags=["config"])
async def read_mode() -> dict:
    return {"config_mode": config_mode()}


@operation(summary="List settings groups with their resolved field values", tags=["config"])
async def read_settings_schema() -> dict:
    """Return every registered settings group with per-field current values.

    Each group carries the settings class' field metadata plus a ``value`` per
    field, resolved with pydantic-settings precedence: ``os.environ`` wins (in
    k8s config mode the cluster injects vars the dotenv store never sees), then
    the stored env override, then the field default. Nested-group reference
    fields (``env_var == ""``) are non-editable and report ``value: null``.
    Secret fields report their real value — this authed surface round-trips
    values through the editor; masking is display-side only, never on the wire.

    Only IMPORTED settings classes appear: in a running skeleton the kit,
    skeleton, and manifest-loaded plugin modules — the intended scope.
    """
    stored = _stored_env()
    groups = []
    for cls_info in registered_settings():
        fields = []
        for field in cls_info.fields:
            payload = field.model_dump()
            default_var = field.default_namespace_var
            if field.env_var and field.env_var in os.environ:
                payload["value"] = os.environ[field.env_var]
            elif field.env_var and field.env_var in stored:
                payload["value"] = stored[field.env_var]
            elif field.env_var and default_var and default_var in os.environ:
                # The field's own var and stored override are both absent, but it
                # participates in the shared ``TAI_DEFAULT_*`` namespace and that layer
                # supplies a value — the truth an operator sees, resolved before the
                # bare field default.
                payload["value"] = os.environ[default_var]
            elif field.env_var and default_var and default_var in stored:
                payload["value"] = stored[default_var]
            elif field.env_var:
                payload["value"] = field.default
            else:
                payload["value"] = None
            fields.append(payload)
        groups.append(
            {
                "name": cls_info.name,
                "module": cls_info.module,
                "qualname": cls_info.qualname,
                "fields": fields,
            }
        )
    return {"groups": groups}


@operation(
    summary="Merge env overrides and hot-reload the process config",
    tags=["config"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=EnvUpdate,
)
async def write_env(env: dict[str, str]) -> dict:
    # Merge the env overrides through the pipeline: ConfigService validates the
    # effective (resolved) config against the backend-needs-bus invariant, writes the
    # env, reloads locally, and broadcasts the reload to the whole fleet. An invalid
    # env key or effective config rejects before anything is written and maps to 400.
    # ``BackendNeedsBusError`` is a RuntimeError (a boot-time refusal must still crash
    # loudly), so the mutate-time path catches it explicitly to map it to a loud,
    # actionable 400 naming TAI_BUS_REDIS_URL rather than letting it escape as a 500.
    try:
        result = await ConfigService.from_app().apply_env_change(env)
    except (ValueError, BackendNeedsBusError) as exc:
        raise BadRequestError(str(exc)) from exc
    return apply_response(result)


@operation(
    summary="Soft-restart the process from its manifest",
    tags=["config"],
    destructive=True,
    reload_gated=True,
    request_model=ReloadConfigRequest,
)
async def reload_config(targets: list[str] | None = None) -> Any:
    """Soft-restart: refresh env from the config manager, reset every settings cache,
    and re-initialize from the manifest — in-process, no pod restart. Applied on this
    worker (when it is a target) and broadcast to the fleet (all workers, or only
    ``targets``); the response embeds the per-worker fleet report.

    Heavy (a full re-init); meant for env/config saves, not tool edits. A convergence
    op: its whole purpose is aligning siblings to persisted state, so a failed local
    reload still broadcasts and then re-raises with the fleet report attached.
    """
    # Run the heavy sync reload on a worker thread through the gate so this call (on
    # the serving loop) does not freeze it.
    return await broadcast(
        {"op": "reload_config"},
        targets,
        lambda: reload_gate.run(tai42_app.admin.reload_config),
        publish_on_local_failure=True,
    )


# -- settings profiles -------------------------------------------------------
#
# CRUD + diff + versions/rollback over the versioned settings_profile store, PLUS the
# C5 apply pipeline (``apply_profile``). A *settings profile* is a named, versioned
# snapshot of the profile-managed env band (``{description, env, secret_keys}``). A save
# (``put_profile``) runs the shared env-write boundary validator over the profile's
# DECLARED env so a profile carrying a deployment X-band key or a dangling ``!ENV`` marker
# is refused at save time (a 400 naming the key), never persisted to be applied later.
# ``apply_profile`` replaces the WHOLE stored env with the profile's env, builds+swaps a
# fresh serving epoch under it (env-write-LAST), broadcasts the reload, recycles the fleet
# for recycle-class diffs, and — for a serve-affecting recycle — arms the applier's own
# deferred graceful self-exit as a post-response BackgroundTask.

# The 501 an OFF-store profile door carries: the versioned-document store is absent,
# so a profile write/read is a capability the deployment lacks (not a transient 503).
_PROFILE_STORE_NOUN = "versioned-document store"
_PROFILE_STORE_CODE = "versioning-not-configured"

# Reserved profile-name prefix — ``@``-prefixed names (e.g. ``@previous``) are the
# apply pipeline's own reserved snapshots; a user may not create one and they are
# excluded from the listing.
_RESERVED_PREFIX = "@"

# The reserved D15 rollback snapshot the apply pipeline saves the pre-apply stored env
# into, so an operator can roll back a bad apply by applying ``@previous``.
_PREVIOUS_NAME = "@previous"


class ProfileRollback(BaseModel):
    """A settings-profile rollback request — the target version to make active."""

    version: int


def _profile_store() -> SettingsProfileStoreView:
    """The active settings-profile store view over the generic versioned store."""
    return settings_profile_store()


def _require_profile_store() -> None:
    """Refuse a profile door cleanly (501) on a store-less deploy — the same
    predicate the preset doors gate on — rather than open an absent Postgres and
    fail with an opaque 500."""
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_PROFILE_STORE_NOUN), extra={"code": _PROFILE_STORE_CODE})


def _reload_class_by_env_var() -> dict[str, str]:
    """Map each registered field's ``env_var`` to its resolved ``reload_class`` — the
    live-registry recycle classification shared with the apply pipeline
    (:func:`~tai42_skeleton.config.boundary.reload_class_by_env_var`)."""
    return reload_class_by_env_var()


@operation(summary="List settings profiles", tags=["config"])
async def list_profiles() -> list[dict[str, Any]]:
    """One ``{name, description}`` row per active settings profile, EXCLUDING the
    reserved ``@``-prefixed snapshots (e.g. ``@previous``). A store-less deploy holds
    no profiles, so it serves an empty list rather than opening an absent store."""
    if not component_store_configured(SKELETON_COMPONENT):
        return []
    store = _profile_store()
    rows: list[dict[str, Any]] = []
    for rec in await store.list_profiles():
        if rec.name.startswith(_RESERVED_PREFIX):
            continue
        try:
            body = await store.get_active_body(rec.name)
        except SettingsProfileNotFoundError:
            # Deleted between the list and the body read — no longer a live row.
            continue
        rows.append({"name": rec.name, "description": body.description})
    return rows


@operation(summary="Get a settings profile", tags=["config"], errors=[NotFoundError, NotSupportedError])
async def get_profile(name: str) -> dict[str, Any]:
    """The profile's active body — ``{description, env, secret_keys}`` with REAL env
    values (this authed ``secret``-fenced door round-trips values through the editor;
    masking is display-side only, never on the wire). 404 for an absent name."""
    _require_profile_store()
    try:
        body = await _profile_store().get_active_body(name)
    except SettingsProfileNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} not found") from exc
    return {"description": body.description, "env": body.env, "secret_keys": body.secret_keys}


@operation(
    summary="Create or update a settings profile",
    tags=["config"],
    errors=[BadRequestError, NotSupportedError],
    request_model=SettingsProfileBody,
)
async def put_profile(name: str, description: str, env: dict[str, str], secret_keys: list[str]) -> dict[str, Any]:
    """Create the profile, or append a new version when it exists (whole-body
    replace). Returns ``{ok: true, version}`` — NEVER the stored body, so a secret
    never re-emits on the write path. A reserved ``@``-prefixed name is a loud 400.
    The profile's DECLARED env is run through the shared boundary validator
    (``ConfigService._validate_replace``): a payload carrying an X-band key or leaving
    a manifest ``!ENV`` marker dangling is refused at save time, naming the key."""
    if name.startswith(_RESERVED_PREFIX):
        raise BadRequestError(
            f"settings profile name {name!r} is reserved: names starting with {_RESERVED_PREFIX!r} are not allowed"
        )
    _require_profile_store()
    body = SettingsProfileBody(description=description, env=env, secret_keys=secret_keys)
    # Refuse a profile that carries a deployment X-band key or a dangling !ENV marker
    # BEFORE it is persisted — the same replace-semantics validation the apply pipeline
    # runs, so an unappliable profile is never committed. ``BackendNeedsBusError`` is a
    # RuntimeError, mapped explicitly to a loud 400 like the env-write door.
    try:
        ConfigService.from_app()._validate_replace(body.env)
    except (ValueError, BackendNeedsBusError) as exc:
        raise BadRequestError(str(exc)) from exc
    store = _profile_store()
    try:
        await store.get_profile(name)
    except SettingsProfileNotFoundError:
        try:
            created = await store.create_profile(name, body)
            return {"ok": True, "version": created.active_version}
        except SettingsProfileExistsError:
            # A concurrent PUT created the profile between the existence probe and this
            # create (the store's active-name unique index rejects the second create).
            # Converge idempotently: fall through to append a version to the now-existing
            # profile rather than surfacing an opaque 500.
            pass
    version = await store.save_version(name, body)
    return {"ok": True, "version": version.version}


@operation(summary="Delete a settings profile", tags=["config"], errors=[NotFoundError, NotSupportedError])
async def delete_profile(name: str) -> dict[str, Any]:
    """Soft-delete the profile, keeping its version history (audit). 404 for an
    absent name."""
    _require_profile_store()
    try:
        await _profile_store().soft_delete(name)
    except SettingsProfileNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} not found") from exc
    return {"ok": True}


@operation(
    summary="Diff a settings profile against the stored env",
    tags=["config"],
    errors=[NotFoundError, NotSupportedError],
)
async def diff_profile(name: str) -> dict[str, Any]:
    """The saved profile's env vs the CURRENT stored env, with REAL values (the UI
    masks) — a preview, not a report. ``added`` / ``removed`` are key names, ``changed``
    is ``[{key, old, new}]``. ``recycle_keys`` are the diff keys whose registry
    ``reload_class`` is ``recycle``; ``refused_keys`` are the diff keys the recycle
    policy refuses upfront on this deployment shape
    (``recycle_policy.capability_report().refused_keys``), named. 404 for an absent
    name."""
    _require_profile_store()
    try:
        body = await _profile_store().get_active_body(name)
    except SettingsProfileNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} not found") from exc
    profile_env = body.env
    current = _stored_env()
    added = sorted(key for key in profile_env if key not in current)
    removed = sorted(key for key in current if key not in profile_env)
    changed = [
        {"key": key, "old": current[key], "new": profile_env[key]}
        for key in sorted(profile_env)
        if key in current and current[key] != profile_env[key]
    ]
    diff_keys = set(added) | set(removed) | {entry["key"] for entry in changed}
    reload_classes = _reload_class_by_env_var()
    recycle_keys = sorted(key for key in diff_keys if reload_classes.get(key) == "recycle")
    refused = set(capability_report().refused_keys)
    refused_keys = sorted(key for key in diff_keys if key in refused)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "recycle_keys": recycle_keys,
        "refused_keys": refused_keys,
    }


@operation(
    summary="List a settings profile's versions",
    tags=["config"],
    errors=[NotFoundError, NotSupportedError],
)
async def list_profile_versions(name: str) -> list[dict[str, Any]]:
    """The profile's version history as a bare array of
    ``{version, tags, created_at, is_current}`` rows — NO body. 404 for an absent
    name."""
    _require_profile_store()
    try:
        versions = await _profile_store().list_versions(name)
    except SettingsProfileNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} not found") from exc
    return [
        {"version": v.version, "tags": v.tags, "created_at": v.created_at, "is_current": v.is_current} for v in versions
    ]


@operation(
    summary="Get a settings profile version",
    tags=["config"],
    errors=[BadRequestError, NotFoundError, NotSupportedError],
)
async def get_profile_version(name: str, version: str) -> dict[str, Any]:
    """One version row extended with its full ``body`` (``{description, env,
    secret_keys}``, REAL values — this door is ``secret``-fenced). A non-integer
    segment is a 400, an unknown version a 404."""
    _require_profile_store()
    try:
        version_num = int(version)
    except ValueError as exc:
        raise BadRequestError("version must be an integer") from exc
    try:
        row = await _profile_store().get_version(name, version_num)
    except SettingsProfileVersionNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} has no version {version_num}") from exc
    return {
        "version": row.version,
        "tags": row.tags,
        "created_at": row.created_at,
        "is_current": row.is_current,
        "body": SettingsProfileBody.model_validate(row.body).model_dump(),
    }


@operation(
    summary="Roll a settings profile back to a version",
    tags=["config"],
    errors=[NotFoundError, NotSupportedError],
    request_model=ProfileRollback,
)
async def rollback_profile(name: str, version: int) -> dict[str, Any]:
    """Re-point the active version to ``version`` (no data copy), making it the
    current body. Returns ``{ok: true, version}``. 404 for an absent name or version.
    A store-only re-point — the live process is realigned by a later apply, not by
    this door."""
    _require_profile_store()
    try:
        record = await _profile_store().rollback(name, version)
    except SettingsProfileVersionNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} has no version {version}") from exc
    return {"ok": True, "version": record.active_version}


async def _save_previous_version(stored_env: dict[str, str]) -> None:
    """Snapshot the CURRENT stored env into the reserved ``@previous`` profile (D15).

    Creates ``@previous`` on the first apply, appends a new version thereafter — the apply
    pipeline's rollback anchor. Carries the current display secret-marks so a rollback
    re-applies with the same masking. Never carries env VALUES onto any report — this is a
    store write, not a response."""
    store = _profile_store()
    body = SettingsProfileBody(
        description="Auto-saved snapshot of the stored env before the last settings-profile apply.",
        env=dict(stored_env),
        secret_keys=list(env_secret_marks_settings().secret_keys),
    )
    try:
        await store.get_profile(_PREVIOUS_NAME)
    except SettingsProfileNotFoundError:
        await store.create_profile(_PREVIOUS_NAME, body)
    else:
        await store.save_version(_PREVIOUS_NAME, body)


@operation(
    summary="Apply a settings profile — replace the stored env and reload the fleet",
    tags=["config"],
    destructive=True,
    reload_gated=True,
    authority_changing=True,
    errors=[BadRequestError, NotFoundError, NotSupportedError],
)
async def apply_profile(name: str) -> OperationResponse:
    """Apply the profile's active env as the WHOLE stored env band (a key the profile
    omits is deleted, save the carried deployment X band), build+swap a fresh serving
    epoch under it, persist env-write-LAST, broadcast the reload, and recycle the fleet
    for recycle-class diffs. NO request body.

    The response is the dedicated ``profileApplyResponse``
    ``{hot, recycle:[{name, kind, status, generation_before}], fresh:[{name, kind,
    generation}], refused:[] on success, fanout}`` — key names + worker identities only,
    never env values. ``recycle`` is one line per recycled/timed-out sibling (plus the
    applier's own deferred self-exit line when it must self-exit); ``fresh`` is the
    per-kind new ready lives observed since the pre-apply snapshot, capacity evidence never
    claimed as any target's successor. A refusal (X-band key, dangling ``!ENV``, a recycle-
    class diff the deployment shape cannot carry) aborts upfront with a loud 400 naming
    the key, before anything is snapshotted, built, or persisted. When the diff carries
    serve-affecting recycle keys the applier's OWN recycle is armed as a post-response
    graceful self-exit (a Starlette ``BackgroundTask``) — its supervisor respawns it on
    the new env. 404 for an absent name."""
    _require_profile_store()
    try:
        body = await _profile_store().get_active_body(name)
    except SettingsProfileNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} not found") from exc
    # Read the reload-driving flag in THIS request context (``EpochAdmissionApp`` set it
    # True for the admitted request): a door-driven apply MUST pass it so the retire
    # excuses this still-admitted request from its in-flight drain rather than self-waiting
    # the full drain budget on it (the PLAN_2 self-deadlock fix).
    driven = _reload_driven_by_request.get()
    try:
        outcome = await ConfigService.from_app().apply_replace_env(
            body.env, driven=driven, save_previous=_save_previous_version
        )
    except (ValueError, BackendNeedsBusError) as exc:
        raise BadRequestError(str(exc)) from exc
    # Arm the applier's own deferred self-exit as a POST-FLUSH BackgroundTask iff the diff
    # carries serve-affecting recycle keys (never a bare shape — refused upfront). An
    # inline ``create_task`` is FORBIDDEN: it would race the response body flush and sever
    # the transport before the report ships.
    background = BackgroundTask(request_serve_graceful_exit) if outcome.serve_affecting else None
    return OperationResponse(profile_apply_response(outcome), background)
