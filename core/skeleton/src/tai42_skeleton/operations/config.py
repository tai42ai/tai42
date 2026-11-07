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
from typing import Any

from pydantic import BaseModel, ConfigDict
from tai42_contract.app import tai42_app
from tai42_contract.app.responses import ApplyResponse
from tai42_contract.errors import UnavailableError
from tai42_kit.settings import present_env_value, registered_settings, settings_field_env_value

from tai42_skeleton.app import instance
from tai42_skeleton.app.boot_rules import translate_config_invariants
from tai42_skeleton.config import config_mode
from tai42_skeleton.config.deployment_env import resolve_deployment_ownership
from tai42_skeleton.config.file_manager import ENV_KEY_RE
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.operations import (
    BadRequestError,
    ConflictError,
    UpstreamError,
    operation,
)
from tai42_skeleton.operations._broadcast import (
    apply_response,
    fleet_reload_response,
)
from tai42_skeleton.operations.response_models.config import (
    ConfigModeView,
    EnvView,
    FleetReloadResponse,
    SettingsSchemaView,
)

# Importing this module registers ``EnvSecretMarksSettings`` (registration runs at
# class-definition time) so the marks group appears in the settings schema, and
# exposes the derived masked-key accessor the env read uses.
from tai42_skeleton.settings.env_secret_marks import (
    SECRET_MARKS_ENV_VAR,
    effective_secret_keys,
)


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
    holds_writer_exit=True,
    errors=[BadRequestError, ConflictError, UnavailableError, UpstreamError],
    request_model=EnvUpdate,
    response_model=ApplyResponse,
)
async def write_env(env: dict[str, str], secret_keys: list[str] | None = None) -> Any:
    """Merge env changes into the stored env and reload this worker and the fleet.

    A hot-class key is re-read in place. A recycle-class key rolls a worker recycle across the
    fleet: the response's ``recycle`` carries the roll's rows (``null`` when the roll had no worker
    to visit: every worker's reload confirmed no recycle-class key moved off its bound value, or the
    only ones naming one were refused at their epoch build or are the writer whose own restart cannot
    follow as its exit), and when the roll converged and this worker needs its own restart, it
    recycles itself after replying — on the HTTP edge and on every projected tool door alike
    (a post-reply graceful self-exit its supervisor respawns). A write run under a bound the roll's
    waits can outlast (a dispatched job's budget, an armed turn budget) hands the roll to the bus
    member it acts for, which carries it out after the reply and departs itself last, once the roll
    converged, when it needs its own restart: ``roll_deferred`` is then ``true`` and
    ``roll_deferred_to`` names that member, ``recycle`` ``null`` and ``awaiting_respawn`` empty.
    A change that cannot take effect is refused with a 400 before anything is written, naming
    the key, under any spelling of its name: a key that locates the worker bus (a bus URL or the
    bus namespace), a deployment / boot-identity (X-band) key no editor may set, and a key the
    deployment sets (set it in the deployment's environment and restart; removing its stored
    copy is allowed).

    ``secret_keys`` (when not ``None``) replaces the operator's secret marks in the same write;
    the marks variable itself is never accepted as an ``env`` key.
    An ``mcp`` selection list whose ``!ENV`` marker would resolve so that a tool something still
    uses stops being picked is refused with a 409 naming the holders, a 503 with ``code``
    ``mcp-server-starting`` while the server's start is in progress or ``mcp-server-not-loaded``
    while this worker has not loaded the server, or a 502 with the recorded cause when this worker
    holds no listing for the server and its last start or bind failed, before anything is written.

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
    return apply_response(result)


@operation(
    summary="Soft-restart the fleet from its manifest, restarting the drifted workers whose reload built",
    tags=["config"],
    destructive=True,
    reload_gated=True,
    holds_writer_exit=True,
    request_model=ReloadConfigRequest,
    response_model=FleetReloadResponse,
)
async def reload_config(targets: list[str] | None = None) -> Any:
    """Soft-restart the fleet from its manifest, then restart the workers whose env needs a restart.

    Refreshes env from the config manager, resets every settings cache, and re-initializes from the
    manifest, in-process. Applied on this worker (when it is a target) and broadcast to the fleet (all
    workers, or only ``targets``); the response embeds the per-worker fleet report.

    Heavy (a full re-init); meant for env/config saves, not tool edits. A convergence
    op: its whole purpose is aligning siblings to persisted state, so a failed local
    reload still broadcasts and then re-raises with the fleet report attached.

    Called over MCP on a worker the reload applies to (no ``targets``, or ``targets`` naming that
    worker), the reload answers on the session that called it and then retires that session with the
    generation it ran on: a later call on the same session fails (streamable HTTP answers it 404, and an
    SSE session's stream closes), so the client starts a new session to go on. When ``targets`` leave
    that worker out, nothing reloads there and the session keeps working.

    An applied worker's payload is ``{status: "ok", env_keys, awaiting_respawn}``; a worker whose
    reload was refused at its epoch build (the build discarded before its swap) replies ``failed``
    with ``{status: "discarded", awaiting_respawn}``, serving its previous epoch with that drift
    standing; any other failed reload, a failed discard among them, carries no payload.
    ``awaiting_respawn`` names the recycle-class keys whose value now differs from the one that
    worker bound (at boot, or when the plugin that declares them was loaded) — the soft restart
    carries every hot key in place, and those keys take effect only on a restart. Once the reports
    are in, every worker the reload addressed that names such a key is restarted one at a time
    through the deployment's supervisor, except a worker whose reload was refused at its epoch
    build: the stored config did not build on it and a restart may fail the same way, so it keeps
    serving its current config and is named in ``awaiting_respawn``. Any worker other than this one
    whose state is unknown (its reload failed otherwise or did not finish, it has no row, its reply
    carries no ``awaiting_respawn`` list, or the bus was unreachable) is asked once it is ready and
    restarted unless its reply confirms it clean or states a refused build (its restart then
    withheld); the workers that confirmed none are left running. This worker is never asked: when
    its own state is unknown, it needs its own restart. ``recycle`` reports that roll (``null`` when
    the roll had no worker to visit: every addressed worker confirmed no recycle-class key, or the
    only ones naming one were refused at their epoch build) and ``awaiting_respawn`` the workers
    still awaiting a respawn after it. When this worker needs its own restart and the roll
    converged, it exits gracefully once the reply is answered and its supervisor starts it again. A
    soft restart run under a bound the roll's waits can outlast (a dispatched job's budget, an armed
    turn budget) runs no roll itself: it hands the roll to the bus member it acts for, which carries
    it out after the reply and departs itself last, once the roll converged, when it needs its own
    restart; ``roll_deferred`` is then ``true``, ``roll_deferred_to`` names that member, ``recycle`` is ``null`` and
    ``awaiting_respawn`` empty, the roll not judged in this reply. Otherwise ``roll_deferred`` is
    ``false`` and ``roll_deferred_to`` ``null``.
    """
    outcome = await ConfigService.from_app().reload_fleet(targets, change="the fleet soft restart")
    return fleet_reload_response(outcome)
