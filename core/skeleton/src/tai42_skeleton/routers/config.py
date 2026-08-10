"""HTTP routes for the deployment config surface — ``/api/config/*``.

AUTHED thin adapters over operations in ``tai42_skeleton.operations.config``:

* ``GET /api/config/env`` — the stored env config plus the operator's secret-key
  marks; admin-only (``action=secret``), the whole store being one admin-owned
  bulk read.
* ``POST /api/config/env`` — merge a ``{key: value}`` env map (all values strings),
  then hot-reload the process config; returns the reload result.
* ``GET /api/config/mode`` — the active config backend mode (``file`` / ``k8s``).
* ``GET /api/config/settings-schema`` — every registered settings group with its
  field metadata and each field's current resolved value; admin-only
  (``action=secret``), the same admin-owned bulk read.
* ``POST /api/config/reload`` — a local soft-restart (refresh env, reset settings
  caches, re-initialize from the manifest), fanned out to every worker when a
  backend is configured.

Success bodies are ``{"data": ...}``; failures are ``{"error": "<message>"}``.
"""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.operations import BadRequestError, operation_metadata_of, register_operation_route
from tai42_skeleton.operations.config import apply_profile as _apply_profile_op
from tai42_skeleton.operations.config import delete_profile as _delete_profile_op
from tai42_skeleton.operations.config import diff_profile as _diff_profile_op
from tai42_skeleton.operations.config import get_profile as _get_profile_op
from tai42_skeleton.operations.config import get_profile_version as _get_profile_version_op
from tai42_skeleton.operations.config import list_profile_versions as _list_profile_versions_op
from tai42_skeleton.operations.config import list_profiles as _list_profiles_op
from tai42_skeleton.operations.config import put_profile as _put_profile_op
from tai42_skeleton.operations.config import read_env as _read_env_op
from tai42_skeleton.operations.config import read_mode as _read_mode_op
from tai42_skeleton.operations.config import read_settings_schema as _read_settings_schema_op
from tai42_skeleton.operations.config import reload_config as _reload_config_op
from tai42_skeleton.operations.config import rollback_profile as _rollback_profile_op
from tai42_skeleton.operations.config import write_env as _write_env_op


async def _extract_env_update(request: Request) -> dict[str, Any]:
    """Parse and validate the env-merge body at the HTTP edge, preserving the door's
    hand-authored 400 messages (a plain request-model parse would answer 422). Yields
    the operation's flat ``env`` kwarg."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object of env values")
    if not all(isinstance(v, str) for v in body.values()):
        raise BadRequestError("all env values must be strings")
    return {"env": body}


async def _extract_profile_body(request: Request) -> dict[str, Any]:
    """Parse and validate a ``SettingsProfileBody`` (``{description, env,
    secret_keys}``) at the HTTP edge, preserving the door's hand-authored 400s (a
    plain request-model parse would answer 422). ``description`` and ``secret_keys``
    default to empty; ``env`` is a ``{str: str}`` map."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object")
    description = body.get("description", "")
    if not isinstance(description, str):
        raise BadRequestError("'description' must be a string")
    env = body.get("env", {})
    if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise BadRequestError("'env' must be a JSON object of string values")
    secret_keys = body.get("secret_keys", [])
    if not isinstance(secret_keys, list) or not all(isinstance(k, str) for k in secret_keys):
        raise BadRequestError("'secret_keys' must be a list of strings")
    return {"description": description, "env": env, "secret_keys": secret_keys}


async def _extract_profile_rollback(request: Request) -> dict[str, Any]:
    """The rollback body → the operation's flat ``version`` kwarg; a missing or
    non-integer ``version`` is a loud 400."""
    try:
        body = await request.json()
    except ValueError as exc:
        raise BadRequestError("invalid JSON body") from exc
    if not isinstance(body, dict):
        raise BadRequestError("body must be a JSON object")
    version = body.get("version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise BadRequestError("body must contain an integer 'version'")
    return {"version": version}


read_env = register_operation_route(
    tai42_app,
    operation_metadata_of(_read_env_op),
    path="/api/config/env",
    method="GET",
    action="secret",
)

write_env = register_operation_route(
    tai42_app,
    operation_metadata_of(_write_env_op),
    path="/api/config/env",
    method="POST",
    context_extractor=_extract_env_update,
    action="fenced",
)

read_mode = register_operation_route(
    tai42_app,
    operation_metadata_of(_read_mode_op),
    path="/api/config/mode",
    method="GET",
    action="read",
)

read_settings_schema = register_operation_route(
    tai42_app,
    operation_metadata_of(_read_settings_schema_op),
    path="/api/config/settings-schema",
    method="GET",
    action="secret",
)

reload_config = register_operation_route(
    tai42_app,
    operation_metadata_of(_reload_config_op),
    path="/api/config/reload",
    method="POST",
    action="fenced",
)

# -- settings profiles --------------------------------------------------------
#
# The action-class per door is PINNED here (never inherited): a list is a plain
# ``read``; reading a profile / a version / the diff exposes REAL secret values, so
# each is the admin-only ``secret`` fence; a save / delete / rollback / apply mutates the
# deployment, so each is the admin-only ``fenced`` mutation. The apply door additionally
# is ``destructive`` + ``reload_gated`` (declared on the operation).

list_profiles = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_profiles_op),
    path="/api/config/profiles",
    method="GET",
    action="read",
)

get_profile = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_profile_op),
    path="/api/config/profiles/{name}",
    method="GET",
    action="secret",
)

put_profile = register_operation_route(
    tai42_app,
    operation_metadata_of(_put_profile_op),
    path="/api/config/profiles/{name}",
    method="PUT",
    context_extractor=_extract_profile_body,
    action="fenced",
)

delete_profile = register_operation_route(
    tai42_app,
    operation_metadata_of(_delete_profile_op),
    path="/api/config/profiles/{name}",
    method="DELETE",
    action="fenced",
)

diff_profile = register_operation_route(
    tai42_app,
    operation_metadata_of(_diff_profile_op),
    path="/api/config/profiles/{name}/diff",
    method="POST",
    action="secret",
)

list_profile_versions = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_profile_versions_op),
    path="/api/config/profiles/{name}/versions",
    method="GET",
    action="read",
)

get_profile_version = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_profile_version_op),
    path="/api/config/profiles/{name}/versions/{version}",
    method="GET",
    action="secret",
)

rollback_profile = register_operation_route(
    tai42_app,
    operation_metadata_of(_rollback_profile_op),
    path="/api/config/profiles/{name}/rollback",
    method="POST",
    context_extractor=_extract_profile_rollback,
    action="fenced",
)

# The C5 apply door — fenced + destructive + reload_gated (the last two declared on the
# operation). NO request body: the profile name in the path is the whole request.
apply_profile = register_operation_route(
    tai42_app,
    operation_metadata_of(_apply_profile_op),
    path="/api/config/profiles/{name}/apply",
    method="POST",
    action="fenced",
)
