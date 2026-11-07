"""Settings-profile operations — ``/api/config/profiles/*``.

CRUD + diff + versions/rollback over the versioned settings_profile store, plus the apply
pipeline (``apply_profile``). A *settings profile* is a named, versioned snapshot of the
profile-managed env band (``{description, env, secret_keys}``):

* ``list_profiles`` / ``get_profile`` / ``put_profile`` / ``delete_profile`` — the profile
  CRUD. A save (``put_profile``) runs the shared env-write boundary validator over the
  profile's DECLARED env so a profile carrying a deployment X-band key or a dangling ``!ENV``
  marker is refused at save time (a 400 naming the key), never persisted to be applied later.
* ``diff_profile`` — the saved profile's env against the current stored env, a preview.
* ``list_profile_versions`` / ``get_profile_version`` / ``rollback_profile`` — the version
  history and the re-point of the active version.
* ``apply_profile`` — replaces the WHOLE stored env with the profile's env, builds+swaps a fresh
  serving epoch under it (env-write-LAST; a process that is not a bus member — a job child a
  backend worker forked — builds the epoch and discards it, the same proof before the write),
  broadcasts the reload, restarts the workers whose reload reports a recycle-class key off its
  bound value (a worker other than the applier whose state is unknown is asked once ready; one
  whose reload was refused at its epoch build — the build discarded before its swap — is left
  running, its restart withheld; the applier is never asked, and when its own state is unknown it
  needs its own restart), and — when the applier needs its own restart and the roll converged —
  owes its deferred graceful self-exit to the edge that answers the call, which runs it after the
  reply.

A store-less deploy refuses every profile door but the list with a 501.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel
from tai42_contract.app.responses import ProfileApplyResponse
from tai42_contract.errors import UnavailableError
from tai42_contract.settings_profiles import PREVIOUS_PROFILE_NAME, RESERVED_PROFILE_PREFIX, SettingsProfileBody
from tai42_contract.settings_profiles.errors import (
    SettingsProfileExistsError,
    SettingsProfileNotFoundError,
    SettingsProfileVersionNotFoundError,
)
from tai42_kit.db import component_store_configured

from tai42_skeleton.app import instance
from tai42_skeleton.app.boot_rules import translate_config_invariants
from tai42_skeleton.config.boundary import EnvKeyClasses, counting_diff_keys
from tai42_skeleton.config.deployment_env import resolve_deployment_ownership
from tai42_skeleton.config.recycle_policy import bus_reaching_keys
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.db import SKELETON_COMPONENT, not_configured_message
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    NotSupportedError,
    UpstreamError,
    operation,
)
from tai42_skeleton.operations._broadcast import profile_apply_response
from tai42_skeleton.operations.config import _stored_env
from tai42_skeleton.operations.response_models.config import (
    OkResult,
    ProfileDiff,
    ProfileListResponse,
    ProfileVersionListResponse,
    ProfileVersionView,
    ProfileWriteResult,
)
from tai42_skeleton.settings.env_secret_marks import SECRET_MARKS_ENV_VAR, merge_secret_marks, parse_secret_marks
from tai42_skeleton.settings_profiles.store import SettingsProfileStoreView, settings_profile_store

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
    boundary validator (``ConfigService.validate_profile_env``): a payload carrying an X-band key, setting a
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
            ownership = await service.deployment_ownership("put_profile")
            await service.validate_profile_env(body.env, ownership)
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
    holds_writer_exit=True,
    authority_changing=True,
    errors=[BadRequestError, ConflictError, NotFoundError, NotSupportedError, UnavailableError, UpstreamError],
    response_model=ProfileApplyResponse,
)
async def apply_profile(name: str) -> Any:
    """Apply the profile's active env as the WHOLE stored env band and reload the fleet.

    The profile's ``secret_keys`` are written as the stored secret marks in the same replace.

    A key the profile omits is deleted, save the carried deployment X band; build+swap a fresh serving
    epoch under it, persist env-write-LAST, broadcast the reload, and recycle the fleet for recycle-class
    diffs. NO request body.

    The response is the dedicated ``profileApplyResponse`` ``{hot, recycle:[{name, kind, status,
    generation_before, keys, detail}], skipped:[name], fresh:[{name, kind, generation}], refused:[],
    recycle_stopped:{kind, name, detail} | null, awaiting_respawn:[{name, keys}], roll_deferred,
    roll_deferred_to, span_release_error, fanout}`` — key names + worker identities only,
    never env values. ``recycle`` is one line per sibling the roll reached (plus the applier's
    own deferred self-exit line when it is armed), its ``keys`` the recycle-class keys that
    worker's reload reported moved off its bound value; ``skipped`` names the workers whose reload
    confirmed no such key, at the reload or when asked once ready, left running. A ``recycled``
    line is confirmed on reality; the roll stops at the first line that is not: ``timed-out``
    (its old life did not go, fresh capacity did not boot, or it never returned to ready, within
    the step budget) or ``failed`` (the worker refused the op, or the bus could not be reached
    while it was in hand), its ``detail`` naming why — or when the bus could not be read before a
    target was reached.
    ``recycle_stopped`` names where the roll stopped and why (``null`` when it ran to its end; a
    ``null`` name when no target was in hand). Later siblings and the applier's own recycle are
    then not attempted, and the call still answers with the report: the env is persisted, and
    ``fanout`` says what the reload broadcast reached. ``fresh`` is the per-kind new ready lives
    observed since the pre-apply snapshot, capacity evidence never claimed as any target's
    successor. ``refused`` is ``[]``:
    a refusal (X-band key, a key the deployment sets, dangling ``!ENV``, a bus-reaching key no
    recycle can carry) aborts upfront with a loud 400 naming the key, before anything is snapshotted, built,
    or persisted. ``awaiting_respawn`` names the workers (name + keys) still awaiting a respawn
    after the roll — when it converged, only the workers whose reload failed at their epoch
    build: the stored config did not build on them and a restart may fail the same way, so the
    roll leaves them running and lists them. ``span_release_error`` is ``null`` when the config
    store released its exclusive write span after the env persisted, otherwise the store's error:
    the env is persisted and the broadcast and roll reported here still ran, but the store may still
    hold its write span until this worker exits. When the applier's own row reports a
    serve-affecting recycle key (or its state is unknown) and the roll converged, the applier's
    OWN recycle is armed as a post-reply graceful self-exit
    — after the HTTP body flushes, or once a projected tool call (MCP, ``tai tools run``, an
    agent, a preset, a schedule, a hook) returned — and its supervisor respawns it on the new
    env. An apply run under a bound the roll's waits can outlast (a dispatched job's budget, an
    armed turn budget) runs no roll itself: it hands the roll to the bus member it acts for, which
    carries it out after the reply and departs itself last, once the roll converged, when it needs
    its own restart; ``roll_deferred`` is then ``true``, ``roll_deferred_to`` names that member, and ``recycle`` and
    ``awaiting_respawn`` are empty, the roll not judged in this reply. Otherwise ``roll_deferred``
    is ``false`` and ``roll_deferred_to`` ``null``. 404 for an absent name.
    An ``mcp`` selection list whose ``!ENV`` marker would resolve so that a tool something still
    uses stops being picked is refused with a 409 naming the holders, a 503 with ``code``
    ``mcp-server-starting`` while the server's start is in progress or ``mcp-server-not-loaded``
    while this worker has not loaded the server, or a 502 with the recorded cause when this worker
    holds no listing for the server and its last start or bind failed, before anything is snapshotted.

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
    return profile_apply_response(outcome)
