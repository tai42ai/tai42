"""Deployment-config operations — ``/api/config/*``.

A thin skin over the config facets (``tai42_app.config``), the admin reload seam
(``tai42_app.admin``), the settings registry, and the worker bus:

* ``read_env`` — the stored env map plus the operator's secret-key marks.
* ``write_env`` — merge a ``{key: value}`` env map (all values strings) through the
  :class:`~tai42_skeleton.config.service.ConfigService` pipeline: validate the effective
  config, write the env, hot-reload the process, and broadcast the reload to the fleet.
* ``read_mode`` — the active config backend mode (``file`` or an external provider's mode).
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
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict
from tai42_contract.app import tai42_app
from tai42_contract.app.responses import ApplyResponse, ProfileApplyResponse
from tai42_contract.settings_profiles import PREVIOUS_PROFILE_NAME, RESERVED_PROFILE_PREFIX, SettingsProfileBody
from tai42_contract.settings_profiles.errors import (
    SettingsProfileExistsError,
    SettingsProfileNotFoundError,
    SettingsProfileVersionNotFoundError,
)
from tai42_kit.db import component_store_configured
from tai42_kit.settings import present_env_value, registered_settings, settings_field_env_value

from tai42_skeleton.app import instance
from tai42_skeleton.app.boot_rules import translate_config_invariants
from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.app.bus.ops import ReloadConfig
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config import config_mode
from tai42_skeleton.config.boundary import EnvKeyClasses, counting_diff_keys
from tai42_skeleton.config.deployment_env import resolve_deployment_ownership
from tai42_skeleton.config.file_manager import ENV_KEY_RE
from tai42_skeleton.config.recycle_policy import bus_reaching_keys
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    OperationResponse,
    UpstreamError,
    operation,
)
from tai42_skeleton.operations._broadcast import (
    apply_response,
    awaiting_respawn_after,
    broadcast,
    log_awaiting_respawn,
    profile_apply_response,
    writer_exit_response,
)
from tai42_skeleton.operations.response_models.config import (
    ConfigModeView,
    EnvView,
    OkResult,
    ProfileDiff,
    ProfileListResponse,
    ProfileVersionListResponse,
    ProfileVersionView,
    ProfileWriteResult,
    SettingsSchemaView,
)

# Importing this module registers ``EnvSecretMarksSettings`` (registration runs at
# class-definition time) so the marks group appears in the settings schema, and
# exposes the derived masked-key accessor the env read uses.
from tai42_skeleton.settings.env_secret_marks import (
    SECRET_MARKS_ENV_VAR,
    effective_secret_keys,
    merge_secret_marks,
    parse_secret_marks,
)
from tai42_skeleton.settings_profiles.store import SettingsProfileStoreView, settings_profile_store


class EnvUpdate(BaseModel):
    """An env write: changes merged into the stored env, and optionally the whole set of secret marks.

    ``env`` maps names to string values (``""`` deletes a name). ``secret_keys`` absent or
    ``null`` leaves the stored marks untouched; a list REPLACES them (``[]`` deletes them).
    """

    model_config = ConfigDict(extra="forbid")

    env: dict[str, str]
    secret_keys: list[str] | None = None


class ReloadConfigRequest(BaseModel):
    """A local reload-config request.

    An optional ``targets`` list restricts the fan-out of the soft-restart to named workers (all workers
    when omitted).
    """

    targets: list[str] | None = None


def _stored_env() -> dict[str, str]:
    """The stored env map, treating a missing ``.env`` as an empty map.

    A never-written env store (``FileNotFoundError``) means "no stored keys",
    not a failure — the caller falls back to the deployment env / defaults.
    """
    try:
        return tai42_app.config.config_manager.read_env()
    except FileNotFoundError:
        return {}


@operation(
    summary="Read the stored env config, secret-key marks and the stored keys the deployment sets",
    tags=["config"],
    response_model=EnvView,
)
async def read_env() -> dict:
    """Return the stored env map alongside the operator's secret-key marks and the deployment-set keys.

    ``data.env`` is the stored env key-value map (a never-written store yields
    an empty map). ``data.secret_keys`` is the DERIVED masked-key set
    (:func:`~tai42_skeleton.settings.env_secret_marks.effective_secret_keys`): the
    operator's stored ``TAI_ENV_SECRET_KEYS`` marks UNIONED with every live
    ``connectors[*].client_secret_env``, so an oauth connector's client secret is
    masked on display even with no operator mark. ``data.deployment_keys`` names the
    stored keys the deployment sets on any worker of the fleet, sorted: a key a worker's
    deployment env sets, or an accepted name of a settings field set under any of its names. Such
    a stored copy never takes effect (the deployment's value wins); a write may remove
    it or carry it unchanged, never set it to a new value. A deployment key with no
    stored copy is not named.
    """
    ownership = await resolve_deployment_ownership(instance.app.bus, "read_env")
    stored = _stored_env()
    return {
        "env": stored,
        "secret_keys": list(effective_secret_keys(tai42_app.admin.live_manifest)),
        "deployment_keys": ownership.owned(stored),
    }


@operation(summary="Read the active config backend mode", tags=["config"], response_model=ConfigModeView)
async def read_mode() -> dict:
    """Return the active config backend mode."""
    return {"config_mode": config_mode()}


@operation(
    summary="List settings groups with their resolved field values",
    tags=["config"],
    response_model=SettingsSchemaView,
)
async def read_settings_schema() -> dict:
    """Return every registered settings group with per-field current values.

    Each group carries the settings class' field metadata plus a ``value`` per
    field, resolved as the answering process resolves it. Each layer reads a field under every
    name that sets it: its accepted names, the JSON name of a nested group it is in, and its
    delimited names. A field the deployment sets on any worker of the fleet, under any such
    name, keeps the deployment's value (its value in this process's env, then the shared
    ``TAI_DEFAULT_*`` namespace, then the field default — a stored copy never applies); a
    worker whose deployment does not set it runs that fallback. Every other field resolves from
    the stored env, then the shared ``TAI_DEFAULT_*`` namespace (process env, then the store),
    then the field default.
    Nested-group reference fields (``env_var == ""``) are non-editable and report
    ``value: null``. Secret fields report their real value — this authed surface
    round-trips values through the editor; masking is display-side only, never on the wire.

    Each field also carries ``value_source`` naming which layer supplied its
    resolved ``value`` so a UI can badge provenance: ``"deployment"`` (the field is
    set by the deployment env; it is locked, and a save that sets it is refused),
    ``"stored"`` (the stored env), ``"default_namespace"`` (a ``TAI_DEFAULT_*``
    fallback, from process env or the store), ``"field_default"`` (the bare field
    default), or ``null`` for a non-editable nested-group reference.

    Only IMPORTED settings classes appear: in a running skeleton the kit,
    skeleton, and manifest-loaded plugin modules — the intended scope.
    """
    ownership = await resolve_deployment_ownership(instance.app.bus, "read_settings_schema")
    stored = _stored_env()
    groups = []
    for cls_info in registered_settings():
        fields = []
        for field in cls_info.fields:
            payload = field.model_dump()
            default_var = field.default_namespace_var
            # A field the deployment sets under any name that sets it (an accepted name, its
            # nested group's JSON name, a delimited name) is the deployment's.
            deployment_set = any(ownership.owns(name) for name in field.accepted_env_vars)
            # Each layer is read as the settings layer reads it (every name that sets the field,
            # empty as absent), so an empty value reports the next layer, as the field actually
            # resolves. The process env holds the deployment env plus the applied stored env, so
            # a field the deployment does not set reads its value from the store.
            stored_value = settings_field_env_value(stored, cls_info.qualname, field) if field.env_var else None
            process_default = present_env_value(os.environ, default_var) if field.env_var and default_var else None
            stored_default = present_env_value(stored, default_var) if field.env_var and default_var else None
            if not field.env_var:
                payload["value"] = None
                payload["value_source"] = None
            elif deployment_set:
                # The process env holds the deployment's value under every name it sets (a stored
                # copy is never applied over it); a field only another worker's deployment sets
                # resolves here to the shared namespace or the field default.
                deployed = settings_field_env_value(os.environ, cls_info.qualname, field)
                if deployed is None:
                    deployed = process_default if process_default is not None else field.default
                payload["value"] = deployed
                payload["value_source"] = "deployment"
            elif stored_value is not None:
                payload["value"] = stored_value
                payload["value_source"] = "stored"
            elif process_default is not None:
                # The field's own var is not stored, but it participates in the shared
                # ``TAI_DEFAULT_*`` namespace and that layer supplies a value — the truth an
                # operator sees, resolved before the bare field default.
                payload["value"] = process_default
                payload["value_source"] = "default_namespace"
            elif stored_default is not None:
                payload["value"] = stored_default
                payload["value_source"] = "default_namespace"
            else:
                payload["value"] = field.default
                payload["value_source"] = "field_default"
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
    summary="Merge env changes into the stored env and reload the fleet",
    tags=["config"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, ConflictError, UpstreamError],
    request_model=EnvUpdate,
    response_model=ApplyResponse,
)
async def write_env(env: dict[str, str], secret_keys: list[str] | None = None) -> OperationResponse:
    """Merge env changes into the stored env and reload this worker and the fleet.

    A hot-class key is re-read in place. A recycle-class key rolls a worker recycle across the
    fleet: the response's ``recycle`` carries the roll's rows (``null`` when the change carried no
    recycle-class key), and when the roll converged this worker recycles itself after replying —
    on the HTTP edge and on every projected tool door alike (a post-reply graceful self-exit its
    supervisor respawns).
    A change that cannot take effect is refused with a 400 before anything is written, naming
    the key, under any spelling of its name: a key that locates the worker bus (a bus URL or the
    bus namespace), a deployment / boot-identity (X-band) key no editor may set, and a key the
    deployment sets (set it in the deployment's environment and restart; removing its stored
    copy is allowed).

    ``secret_keys`` (when not ``None``) replaces the operator's secret marks in the same write;
    the marks variable itself is never accepted as an ``env`` key.
    An ``mcp`` selection list whose ``!ENV`` marker would resolve so that a tool something still
    uses stops being picked is refused with a 409 naming the holders, or a 502 when the server
    cannot list its tools for that check, before anything is written.

    Called over MCP, the reload answers on the session that called it and then retires that session with
    the generation it ran on: a later call on the same session fails (streamable HTTP answers it 404,
    and an SSE session's stream closes), so the client starts a new session to go on.
    """
    if SECRET_MARKS_ENV_VAR in env:
        raise BadRequestError("set secret marks through the 'secret_keys' field, not as an env key")
    for mark in secret_keys or []:
        if not ENV_KEY_RE.fullmatch(mark):
            raise BadRequestError(f"secret_keys entry {mark!r} is not a valid env key")
    # Merge the env changes through the pipeline: ConfigService validates the
    # effective (resolved) config against the backend-needs-bus invariant, writes the
    # env, reloads locally, and broadcasts the reload to the whole fleet. An invalid
    # env key or effective config rejects before anything is written and maps to 400.
    with translate_config_invariants(BadRequestError):
        try:
            result = await ConfigService.from_app().apply_env_change(env, secret_keys)
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
    return writer_exit_response(apply_response(result), armed=result.self_exit_armed)


@operation(
    summary="Soft-restart the process from its manifest",
    tags=["config"],
    destructive=True,
    reload_gated=True,
    request_model=ReloadConfigRequest,
    response_model=FleetResult,
)
async def reload_config(targets: list[str] | None = None) -> Any:
    """Soft-restart the process from its manifest, in-process with no pod restart.

    Refreshes env from the config manager, resets every settings cache, and re-initializes from the
    manifest. Applied on this worker (when it is a target) and broadcast to the fleet (all workers, or only
    ``targets``); the response embeds the per-worker fleet report.

    Heavy (a full re-init); meant for env/config saves, not tool edits. A convergence
    op: its whole purpose is aligning siblings to persisted state, so a failed local
    reload still broadcasts and then re-raises with the fleet report attached.

    Called over MCP on a worker the reload applies to (no ``targets``, or ``targets`` naming that
    worker), the reload answers on the session that called it and then retires that session with the
    generation it ran on: a later call on the same session fails (streamable HTTP answers it 404, and an
    SSE session's stream closes), so the client starts a new session to go on. When ``targets`` leave
    that worker out, nothing reloads there and the session keeps working.

    Each worker's payload is ``{status, env_keys, awaiting_respawn}``: ``awaiting_respawn`` names the
    recycle-class keys whose value now differs from the one that worker bound (at boot, or when the
    plugin that declares them was loaded) — the soft
    restart carries every hot key in place, and those keys take effect only when the worker is
    respawned through the deployment's supervisor. This
    operation ERROR-logs one line naming every such worker and its keys.
    """
    # Run the heavy sync reload on a worker thread through the gate so this call (on
    # the serving loop) does not freeze it.
    report = await broadcast(
        ReloadConfig(op="reload_config"),
        targets,
        lambda: reload_gate.run(tai42_app.admin.reload_config, reimports=True),
        publish_on_local_failure=True,
    )
    # This operation starts the reload and rolls nothing, so every row stating a moved
    # recycle-class env is a worker awaiting a respawn: one loud line naming them.
    log_awaiting_respawn(awaiting_respawn_after(FleetResult.model_validate(report), None, None, self_exit_armed=False))
    return report


# -- settings profiles -------------------------------------------------------
#
# CRUD + diff + versions/rollback over the versioned settings_profile store, PLUS the
# apply pipeline (``apply_profile``). A *settings profile* is a named, versioned
# snapshot of the profile-managed env band (``{description, env, secret_keys}``). A save
# (``put_profile``) runs the shared env-write boundary validator over the profile's
# DECLARED env so a profile carrying a deployment X-band key or a dangling ``!ENV`` marker
# is refused at save time (a 400 naming the key), never persisted to be applied later.
# ``apply_profile`` replaces the WHOLE stored env with the profile's env, builds+swaps a
# fresh serving epoch under it (env-write-LAST; a process that is not a bus member — a job
# child a backend worker forked — builds the epoch and discards it, the same proof before the
# write), broadcasts the reload, recycles the fleet
# for recycle-class diffs, and — for a serve-affecting recycle — arms the applier's own
# deferred graceful self-exit as a post-reply background task.

# The 501 an OFF-store profile door carries: the versioned-document store is absent,
# so a profile write/read is a capability the deployment lacks (not a transient 503).
_PROFILE_STORE_NOUN = "versioned-document store"
_PROFILE_STORE_CODE = "versioning-not-configured"


class ProfileRollback(BaseModel):
    """A settings-profile rollback request — the target version to make active."""

    version: int


def _profile_store() -> SettingsProfileStoreView:
    """The active settings-profile store view over the generic versioned store."""
    return settings_profile_store()


def _require_profile_store() -> None:
    """Refuse a profile door cleanly (501) on a store-less deploy.

    The same predicate the preset doors gate on — rather than open an absent Postgres and fail with an
    opaque 500.
    """
    if not component_store_configured(SKELETON_COMPONENT):
        raise NotSupportedError(not_configured_message(_PROFILE_STORE_NOUN), extra={"code": _PROFILE_STORE_CODE})


def _recycle_class_keys(keys: Iterable[str]) -> list[str]:
    """The ``recycle``-class keys of ``keys``, sorted.

    The live-registry classification shared with the apply pipeline
    (:meth:`~tai42_skeleton.config.boundary.EnvKeyClasses.reload_class`).
    """
    classes = EnvKeyClasses()
    return sorted(key for key in keys if classes.reload_class(key) == "recycle")


@operation(summary="List settings profiles", tags=["config"], response_model=ProfileListResponse)
async def list_profiles() -> list[dict[str, Any]]:
    """One ``{name, description}`` row per active settings profile, excluding reserved ``@``-prefixed snapshots.

    Reserved snapshots include e.g. ``@previous``. A store-less deploy holds no profiles, so it serves an
    empty list rather than opening an absent store.
    """
    if not component_store_configured(SKELETON_COMPONENT):
        return []
    store = _profile_store()
    rows: list[dict[str, Any]] = []
    for rec in await store.list_profiles():
        if rec.name.startswith(RESERVED_PROFILE_PREFIX):
            continue
        try:
            body = await store.get_active_body(rec.name)
        except SettingsProfileNotFoundError:
            # Deleted between the list and the body read — no longer a live row.
            continue
        rows.append({"name": rec.name, "description": body.description})
    return rows


@operation(
    summary="Get a settings profile",
    tags=["config"],
    errors=[NotFoundError, NotSupportedError],
    response_model=SettingsProfileBody,
)
async def get_profile(name: str) -> dict[str, Any]:
    """The profile's active body — ``{description, env, secret_keys}`` with REAL env values.

    This authed ``secret``-fenced door round-trips values through the editor; masking is display-side only,
    never on the wire. 404 for an absent name.
    """
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
    response_model=ProfileWriteResult,
)
async def put_profile(name: str, description: str, env: dict[str, str], secret_keys: list[str]) -> dict[str, Any]:
    """Create the profile, or append a new version when it exists (whole-body replace).

    Returns ``{ok: true, version}`` — NEVER the stored body, so a secret never re-emits on the write path.
    A reserved ``@``-prefixed name is a loud 400. The profile's DECLARED env is run through the shared
    boundary validator (``ConfigService._validate_replace``): a payload carrying an X-band key, setting a
    key the deployment sets to a value other than its stored one, or leaving a manifest ``!ENV`` marker
    dangling is refused at save time, naming the key.
    """
    if name.startswith(RESERVED_PROFILE_PREFIX):
        raise BadRequestError(
            f"settings profile name {name!r} is reserved: "
            f"names starting with {RESERVED_PROFILE_PREFIX!r} are not allowed"
        )
    _require_profile_store()
    body = SettingsProfileBody(description=description, env=env, secret_keys=secret_keys)
    # Refuse a profile that carries a deployment X-band key, sets a key the deployment sets,
    # or leaves a dangling !ENV marker BEFORE it is persisted — the same replace-semantics validation the apply pipeline
    # runs, so an unappliable profile is never committed.
    with translate_config_invariants(BadRequestError):
        try:
            service = ConfigService.from_app()
            service._validate_replace(body.env, await service.deployment_ownership("put_profile"))
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
    store = _profile_store()
    try:
        await store.get_profile(name)
    except SettingsProfileNotFoundError:
        try:
            created = await store.create_profile(name, body)
        except SettingsProfileExistsError:
            # A concurrent PUT created the profile between the existence probe and this
            # create (the store's active-name unique index rejects the second create).
            # Converge idempotently: fall through to append a version to the now-existing
            # profile rather than surfacing an opaque 500.
            pass
        else:
            return {"ok": True, "version": created.active_version}
    version = await store.save_version(name, body)
    return {"ok": True, "version": version.version}


@operation(
    summary="Delete a settings profile",
    tags=["config"],
    errors=[NotFoundError, NotSupportedError],
    response_model=OkResult,
)
async def delete_profile(name: str) -> dict[str, Any]:
    """Soft-delete the profile, keeping its version history (audit).

    404 for an absent name.
    """
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
    response_model=ProfileDiff,
)
async def diff_profile(name: str) -> dict[str, Any]:
    """The saved profile's env vs the CURRENT stored env, with REAL values (the UI masks) — a preview, not a report.

    ``added`` / ``removed`` are key names, ``changed`` is ``[{key, old, new}]``: the stored-row diff,
    a stored copy of a key the deployment sets included. ``recycle_keys`` and the bus-reaching part of
    ``refused_keys`` are read from the change the reload really produces, as the apply classifies it
    (a key the deployment sets is never part of it): ``recycle_keys`` are the changed keys whose
    registry ``reload_class`` is ``recycle``; ``refused_keys`` are the keys the apply would refuse
    upfront, named: the changed bus-reaching keys, and the keys the deployment sets that the profile
    sets to a value other than the stored one. 404 for an absent name.
    """
    _require_profile_store()
    try:
        body = await _profile_store().get_active_body(name)
    except SettingsProfileNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} not found") from exc
    profile_env = _profile_env(body)
    ownership = await resolve_deployment_ownership(instance.app.bus, "diff_profile")
    current = _stored_env()
    added = sorted(key for key in profile_env if key not in current)
    removed = sorted(key for key in current if key not in profile_env)
    changed = [
        {"key": key, "old": current[key], "new": profile_env[key]}
        for key in sorted(profile_env)
        if key in current and current[key] != profile_env[key]
    ]
    # The apply classifies the change the reload really produces, the keys the deployment sets
    # dropped from both sides; the preview's recycle and bus-key callouts read the same diff.
    diff_keys = counting_diff_keys(current, profile_env, ownership)
    recycle_keys = _recycle_class_keys(diff_keys)
    refused_keys = sorted({*bus_reaching_keys(diff_keys), *ownership.changed_writes(profile_env, current)})
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "recycle_keys": recycle_keys,
        "refused_keys": refused_keys,
    }


def _profile_env(body: SettingsProfileBody) -> dict[str, str]:
    """The env band applying ``body`` writes: its env plus its ``secret_keys`` as the secret marks.

    The marks travel in ``secret_keys`` (a profile's env rows never carry the marks variable), so the
    replace writes them with the env and a marked key stays masked once applied.
    """
    env = dict(body.env)
    marks = merge_secret_marks(env.get(SECRET_MARKS_ENV_VAR), body.secret_keys)
    if marks:
        env[SECRET_MARKS_ENV_VAR] = marks
    return env


@operation(
    summary="List a settings profile's versions",
    tags=["config"],
    errors=[NotFoundError, NotSupportedError],
    response_model=ProfileVersionListResponse,
)
async def list_profile_versions(name: str) -> list[dict[str, Any]]:
    """The profile's version history as a bare array of ``{version, tags, created_at, is_current}`` rows — NO body.

    404 for an absent name.
    """
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
    response_model=ProfileVersionView,
)
async def get_profile_version(name: str, version: str) -> dict[str, Any]:
    """One version row extended with its full ``body`` (REAL values — this door is ``secret``-fenced).

    The body is ``{description, env, secret_keys}``. A non-integer segment is a 400, an unknown version
    a 404.
    """
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
    response_model=ProfileWriteResult,
)
async def rollback_profile(name: str, version: int) -> dict[str, Any]:
    """Re-point the active version to ``version`` (no data copy), making it the current body.

    Returns ``{ok: true, version}``. 404 for an absent name or version. A store-only re-point — the live
    process is realigned by a later apply, not by this door.
    """
    _require_profile_store()
    try:
        record = await _profile_store().rollback(name, version)
    except SettingsProfileVersionNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} has no version {version}") from exc
    return {"ok": True, "version": record.active_version}


async def _save_previous_version(stored_env: dict[str, str]) -> None:
    """Snapshot the stored env band ``stored_env`` into the reserved ``@previous`` profile.

    Creates ``@previous`` on the first apply, appends a new version thereafter — the apply
    pipeline's rollback anchor. The apply hands the stored env that counted before it (the
    stored keys the deployment does not set), so applying ``@previous`` restores every key
    that counted and sets no deployment key, as the deployment env stood at that apply; a key
    the deployment sets later is refused on ``@previous`` as on any profile. The snapshot has
    the shape of every other profile: ``env`` is the band's rows without the secret-marks
    variable, ``secret_keys`` the band's own stored marks, so applying it (:func:`_profile_env`)
    writes the band back exactly. Never carries env VALUES onto any report — this is a store
    write, not a response.
    """
    store = _profile_store()
    body = SettingsProfileBody(
        description="Auto-saved snapshot of the stored env before the last settings-profile apply.",
        env={key: value for key, value in stored_env.items() if key != SECRET_MARKS_ENV_VAR},
        secret_keys=parse_secret_marks(stored_env.get(SECRET_MARKS_ENV_VAR)),
    )
    try:
        await store.get_profile(PREVIOUS_PROFILE_NAME)
    except SettingsProfileNotFoundError:
        await store.create_profile(PREVIOUS_PROFILE_NAME, body)
    else:
        await store.save_version(PREVIOUS_PROFILE_NAME, body)


@operation(
    summary="Apply a settings profile — replace the stored env and reload the fleet",
    tags=["config"],
    destructive=True,
    reload_gated=True,
    authority_changing=True,
    errors=[BadRequestError, ConflictError, NotFoundError, NotSupportedError, UpstreamError],
    response_model=ProfileApplyResponse,
)
async def apply_profile(name: str) -> OperationResponse:
    """Apply the profile's active env as the WHOLE stored env band and reload the fleet.

    The profile's ``secret_keys`` are written as the stored secret marks in the same replace.

    A key the profile omits is deleted, save the carried deployment X band; build+swap a fresh serving
    epoch under it, persist env-write-LAST, broadcast the reload, and recycle the fleet for recycle-class
    diffs. NO request body.

    The response is the dedicated ``profileApplyResponse`` ``{hot, recycle:[{name, kind, status,
    generation_before, detail}], fresh:[{name, kind, generation}], refused:[],
    recycle_stopped:{kind, name, detail} | null, awaiting_respawn:[{name, keys}], span_release_error, fanout}``
    — key names + worker identities only,
    never env values. ``recycle`` is one line per sibling the roll reached (plus the applier's
    own deferred self-exit line when it is armed). A ``recycled`` line is confirmed on reality;
    the roll stops at the first line that is not: ``timed-out`` (its old life did not go, fresh
    capacity did not boot, or it never returned to ready, within the step budget) or ``failed``
    (the worker refused the op, or the bus could not be reached while it was in hand), its
    ``detail`` naming why — or when the bus could not be read before a target was reached.
    ``recycle_stopped`` names where the roll stopped and why (``null`` when it ran to its end; a
    ``null`` name when no target was in hand). Later siblings and the applier's own recycle are
    then not attempted, and the call still answers with the report: the env is persisted and the
    fleet reloaded. ``fresh`` is the per-kind new ready lives observed since the pre-apply
    snapshot, capacity evidence never claimed as any target's successor. ``refused`` is ``[]``:
    a refusal (X-band key, a key the deployment sets, dangling ``!ENV``, a bus-reaching key no
    recycle can carry) aborts upfront with a loud 400 naming the key, before anything is snapshotted, built,
    or persisted. ``awaiting_respawn`` names the workers (name + keys) still awaiting a respawn
    after the roll — empty when it converged. ``span_release_error`` is ``null`` when the config
    store released its exclusive write span after the env persisted, otherwise the store's error:
    the env is persisted and the broadcast and roll reported here still ran, but the store may still
    hold its write span until this worker exits. When the diff carries serve-affecting recycle keys
    and the roll converged, the applier's OWN recycle is armed as a post-reply graceful self-exit
    — after the HTTP body flushes, or once a projected tool call (MCP, ``tai tools run``, an
    agent, a preset, a schedule, a hook) returned — and its supervisor respawns it on the new
    env. 404 for an absent name.
    An ``mcp`` selection list whose ``!ENV`` marker would resolve so that a tool something still
    uses stops being picked is refused with a 409 naming the holders, or a 502 when the server
    cannot list its tools for that check, before anything is snapshotted.

    Called over MCP, the reload answers on the session that called it and then retires that session with
    the generation it ran on: a later call on the same session fails (streamable HTTP answers it 404,
    and an SSE session's stream closes), so the client starts a new session to go on.
    """
    _require_profile_store()
    try:
        body = await _profile_store().get_active_body(name)
    except SettingsProfileNotFoundError as exc:
        raise NotFoundError(f"settings profile {name!r} not found") from exc
    with translate_config_invariants(BadRequestError):
        try:
            outcome = await ConfigService.from_app().apply_replace_env(
                _profile_env(body), save_previous=_save_previous_version
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
    # Arm the applier's own deferred self-exit as a post-reply background task iff the diff
    # carries serve-affecting recycle keys and the roll converged. The edge runs it after its
    # reply (``OperationResponse``).
    return writer_exit_response(profile_apply_response(outcome), armed=outcome.self_exit_armed)
