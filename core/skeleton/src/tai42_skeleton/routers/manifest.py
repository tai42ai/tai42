"""Manifest + MCP HTTP surface for the Studio manifest feature.

AUTHED thin adapters over operations in ``tai42_skeleton.operations.manifest`` (the
live manifest can embed connector tokens in MCP headers/env, so this whole surface
stays behind the credential):

- ``GET  /api/manifest``                     — the PRESERVED manifest MCP section + user tools (markers intact).
- ``GET  /api/manifest/preserved``           — the PRESERVED manifest view the McpTab editor reads.
- ``GET  /api/manifest/mcp-env-refs``         — the MCP section's !ENV marker refs (names + set/unset only).
- ``POST /api/manifest/replace``             — replace the WHOLE persisted manifest, fleet-wide (tier-2).
- ``POST /api/mcp-config``                   — replace the MCP section (persist + reload).
- ``POST /api/mcp-config/entries``           — add/replace MCP entries by title (persist + reload).
- ``DELETE /api/mcp-config/entries/{title}`` — remove one MCP entry by title (persist + reload).
- ``POST /api/tools-config/entries``         — add/replace tools entries by title (persist + reload).
- ``DELETE /api/tools-config/entries/{title}``— remove one tools entry by title (persist + reload).
- ``POST /api/agents-config/entries``        — add/replace agents entries by title (persist + reload).
- ``DELETE /api/agents-config/entries/{title}``— remove one agents entry by title (persist + reload).
- ``POST /api/api-tools``                    — add/remove names on the api_tools include/exclude lists (tier-2).
- ``POST /api/mcp-config/secret-env``        — write a secret env value + its manifest !ENV marker together.
- ``GET  /api/mcp-config/schema``            — the JSON Schema for one MCP-config entry.
- ``GET  /api/mcp-status``                   — live MCP binding snapshot.
- ``GET  /api/mcp-status/failed``            — the MCP servers skipped by the viability check.
- ``POST /api/mcp-status/reload-failed``     — re-probe every failed MCP server.
- ``POST /api/mcp-status/{title}/reload``    — reload a single MCP server by title.
- ``POST /api/mcp-status/{title}/deregister``— detach a single MCP server's tools by title.

There is no ``probe-mcp`` route: the skeleton has no MCP-probe primitive, only the
connectors sub-service probe.

Success bodies are ``{"data": ...}``; failures are ``{"error": "<message>"}``.
"""

from __future__ import annotations

from json import JSONDecodeError
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.operations import BadRequestError, operation_metadata_of, register_operation_route
from tai42_skeleton.operations.manifest import ManifestReplace, SetMcpSecretEnv
from tai42_skeleton.operations.manifest import add_agents_entries as _add_agents_entries_op
from tai42_skeleton.operations.manifest import add_mcp_entries as _add_mcp_entries_op
from tai42_skeleton.operations.manifest import add_tools_entries as _add_tools_entries_op
from tai42_skeleton.operations.manifest import deregister_mcp as _deregister_mcp_op
from tai42_skeleton.operations.manifest import get_manifest as _get_manifest_op
from tai42_skeleton.operations.manifest import get_manifest_preserved as _get_manifest_preserved_op
from tai42_skeleton.operations.manifest import get_mcp_config_schema as _get_mcp_config_schema_op
from tai42_skeleton.operations.manifest import get_mcp_env_refs as _get_mcp_env_refs_op
from tai42_skeleton.operations.manifest import get_mcp_status as _get_mcp_status_op
from tai42_skeleton.operations.manifest import list_failed_mcps as _list_failed_mcps_op
from tai42_skeleton.operations.manifest import reload_failed_mcps as _reload_failed_mcps_op
from tai42_skeleton.operations.manifest import reload_mcp as _reload_mcp_op
from tai42_skeleton.operations.manifest import remove_agents_entry as _remove_agents_entry_op
from tai42_skeleton.operations.manifest import remove_mcp_entry as _remove_mcp_entry_op
from tai42_skeleton.operations.manifest import remove_tools_entry as _remove_tools_entry_op
from tai42_skeleton.operations.manifest import set_mcp_config as _set_mcp_config_op
from tai42_skeleton.operations.manifest import set_mcp_secret_env as _set_mcp_secret_env_op
from tai42_skeleton.operations.manifest import update_api_tools as _update_api_tools_op
from tai42_skeleton.operations.manifest import update_manifest as _update_manifest_op


async def _json_object(request: Request) -> dict:
    """Read a JSON-object request body, mapping a malformed/non-object body to the
    door's explicit 400 (a plain request-model parse would answer 422)."""
    try:
        body = await request.json()
    except (JSONDecodeError, ValueError) as exc:
        raise BadRequestError(str(exc)) from exc
    if not isinstance(body, dict):
        raise BadRequestError("request body must be a JSON object")
    return body


async def _extract_mcp_config(request: Request) -> dict[str, Any]:
    """The MCP-config replacement body → the operation's flat ``mcp`` kwarg. The
    hand-authored 400s (malformed body, missing ``mcp`` list) are preserved here;
    a malformed ENTRY is validated by the operation (also a 400)."""
    body = await _json_object(request)
    if "mcp" not in body:
        raise BadRequestError("body must carry an 'mcp' list")
    return {"mcp": body["mcp"]}


def _entries_kwargs(body: dict) -> dict[str, Any]:
    """A shared add-entries body → op kwargs: an ``entries`` list (missing key or non-list
    → loud 400) plus an optional ``replace`` flag (absent → ``False``; present non-bool →
    loud 400 — an explicit bool check, never a truthy coercion). Emptiness is the op's check."""
    entries = body.get("entries")
    if not isinstance(entries, list):
        raise BadRequestError("body must carry an 'entries' list")
    replace = body.get("replace", False)
    if not isinstance(replace, bool):
        raise BadRequestError("'replace' must be a boolean")
    return {"entries": entries, "replace": replace}


async def _extract_mcp_entries(request: Request) -> dict[str, Any]:
    return _entries_kwargs(await _json_object(request))


async def _extract_tools_entries(request: Request) -> dict[str, Any]:
    return _entries_kwargs(await _json_object(request))


async def _extract_agents_entries(request: Request) -> dict[str, Any]:
    return _entries_kwargs(await _json_object(request))


def _name_list(body: dict, field: str) -> list[str]:
    """One ``api_tools`` name-list field: absent → ``[]``; present and not a list of
    strings → loud 400 naming the field."""
    value = body.get(field, [])
    if not isinstance(value, list) or not all(isinstance(name, str) for name in value):
        raise BadRequestError(f"'{field}' must be a list of strings")
    return value


async def _extract_api_tools_lists(request: Request) -> dict[str, Any]:
    """The api_tools list-edit body → the op's four flat name-list kwargs. Emptiness (all
    four empty) is the op's check."""
    body = await _json_object(request)
    fields = ("include_add", "include_remove", "exclude_add", "exclude_remove")
    return {field: _name_list(body, field) for field in fields}


async def _optional_targets(request: Request) -> list[str] | None:
    """The optional ``targets`` fan-out restriction from a POST body, tolerating an
    absent/empty body (no body → ``targets=None`` → the unchanged single-worker path)."""
    try:
        body = await request.json()
    except (JSONDecodeError, ValueError):
        return None
    if not isinstance(body, dict):
        return None
    return body.get("targets")


async def _extract_targets(request: Request) -> dict[str, Any]:
    """A body carrying only the optional ``targets`` fan-out restriction (reload,
    reload-failed, deregister); ``title`` is a path param the adapter supplies."""
    return {"targets": await _optional_targets(request)}


async def _extract_failed_query(request: Request) -> dict[str, Any]:
    """The optional ``targets`` fan-out restriction from the query string (a GET
    carries it as a repeated ``?targets=`` param, never a body); absent → ``None``."""
    targets = request.query_params.getlist("targets")
    return {"targets": targets or None}


async def _extract_secret_env(request: Request) -> dict[str, Any]:
    """The combined secret-env body → the operation's flat kwargs. Preserves the door's
    hand-authored 400s (a mistyped field would otherwise answer 422): ``value`` and
    ``manifest_pointer`` are required; the env KEY is EITHER an explicit ``key`` OR a
    ``key_hint`` (exactly one — the op enforces the choice and refuses a colliding key)."""
    body = await _json_object(request)
    try:
        model = SetMcpSecretEnv.model_validate(body)
    except ValidationError as exc:
        raise BadRequestError(f"invalid secret-env body: {exc}") from exc
    return {
        "value": model.value,
        "key": model.key,
        "key_hint": model.key_hint,
        "manifest_pointer": model.manifest_pointer,
    }


async def _extract_manifest_replace(request: Request) -> dict[str, Any]:
    """The full-manifest replacement body → the operation's flat ``manifest_text``.

    The body carries the manifest TEXT verbatim (the PRESERVED view — ``!ENV`` markers
    intact); the operation loads and validates it. A body missing ``manifest_text`` (or
    a non-string one) is a loud 400 rather than the adapter's default 422."""
    body = await _json_object(request)
    try:
        model = ManifestReplace.model_validate(body)
    except ValidationError as exc:
        raise BadRequestError(f"invalid manifest: {exc}") from exc
    return {"manifest_text": model.manifest_text}


get_manifest = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_manifest_op),
    path="/api/manifest",
    method="GET",
    action="read",
)

get_manifest_preserved = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_manifest_preserved_op),
    path="/api/manifest/preserved",
    method="GET",
    action="read",
)

update_manifest = register_operation_route(
    tai42_app,
    operation_metadata_of(_update_manifest_op),
    path="/api/manifest/replace",
    method="POST",
    context_extractor=_extract_manifest_replace,
    action="fenced",
)

set_mcp_config = register_operation_route(
    tai42_app,
    operation_metadata_of(_set_mcp_config_op),
    path="/api/mcp-config",
    method="POST",
    context_extractor=_extract_mcp_config,
    action="write",
)

add_mcp_entries = register_operation_route(
    tai42_app,
    operation_metadata_of(_add_mcp_entries_op),
    path="/api/mcp-config/entries",
    method="POST",
    context_extractor=_extract_mcp_entries,
    action="write",
)

remove_mcp_entry = register_operation_route(
    tai42_app,
    operation_metadata_of(_remove_mcp_entry_op),
    path="/api/mcp-config/entries/{title}",
    method="DELETE",
    action="write",
)

add_tools_entries = register_operation_route(
    tai42_app,
    operation_metadata_of(_add_tools_entries_op),
    path="/api/tools-config/entries",
    method="POST",
    context_extractor=_extract_tools_entries,
    action="write",
)

remove_tools_entry = register_operation_route(
    tai42_app,
    operation_metadata_of(_remove_tools_entry_op),
    path="/api/tools-config/entries/{title}",
    method="DELETE",
    action="write",
)

add_agents_entries = register_operation_route(
    tai42_app,
    operation_metadata_of(_add_agents_entries_op),
    path="/api/agents-config/entries",
    method="POST",
    context_extractor=_extract_agents_entries,
    action="write",
)

remove_agents_entry = register_operation_route(
    tai42_app,
    operation_metadata_of(_remove_agents_entry_op),
    path="/api/agents-config/entries/{title}",
    method="DELETE",
    action="write",
)

update_api_tools = register_operation_route(
    tai42_app,
    operation_metadata_of(_update_api_tools_op),
    path="/api/api-tools",
    method="POST",
    context_extractor=_extract_api_tools_lists,
    action="fenced",
)

set_mcp_secret_env = register_operation_route(
    tai42_app,
    operation_metadata_of(_set_mcp_secret_env_op),
    path="/api/mcp-config/secret-env",
    method="POST",
    context_extractor=_extract_secret_env,
    action="fenced",
)

get_mcp_env_refs = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_mcp_env_refs_op),
    path="/api/manifest/mcp-env-refs",
    method="GET",
    action="read",
)

get_mcp_config_schema = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_mcp_config_schema_op),
    path="/api/mcp-config/schema",
    method="GET",
    action="read",
)

get_mcp_status = register_operation_route(
    tai42_app,
    operation_metadata_of(_get_mcp_status_op),
    path="/api/mcp-status",
    method="GET",
    action="read",
)

list_failed_mcps = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_failed_mcps_op),
    path="/api/mcp-status/failed",
    method="GET",
    context_extractor=_extract_failed_query,
    action="read",
)

reload_failed_mcps = register_operation_route(
    tai42_app,
    operation_metadata_of(_reload_failed_mcps_op),
    path="/api/mcp-status/reload-failed",
    method="POST",
    context_extractor=_extract_targets,
    action="fenced",
)

reload_mcp = register_operation_route(
    tai42_app,
    operation_metadata_of(_reload_mcp_op),
    path="/api/mcp-status/{title}/reload",
    method="POST",
    context_extractor=_extract_targets,
    action="write",
)

deregister_mcp = register_operation_route(
    tai42_app,
    operation_metadata_of(_deregister_mcp_op),
    path="/api/mcp-status/{title}/deregister",
    method="POST",
    context_extractor=_extract_targets,
    action="fenced",
)
