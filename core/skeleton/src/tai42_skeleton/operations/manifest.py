"""Manifest + MCP-status operations — ``/api/manifest*``, ``/api/mcp-config*``,
``/api/mcp-status*``.

A thin skin over the live-manifest admin surface (``tai42_app.admin``), the config
manager, the reload gate, and the worker bus (``instance.app.bus``). Two groups:

Reads (each returns its shape directly; the adapter envelopes it):

* ``get_manifest`` — the PRESERVED persisted manifest's MCP section + user tools
  (``!ENV`` markers intact — the no-leak retighten; the resolved read is retired).
* ``get_manifest_preserved`` — the same preserved ``{mcp, user_tools}`` view behind the
  explicit ``/api/manifest/preserved`` door the Studio McpTab editor reads.
* ``get_mcp_config_schema`` — the JSON Schema for one MCP-config entry.
* ``get_mcp_status`` — the live MCP binding snapshot.
* ``list_failed_mcps`` — the MCP servers skipped by the viability check; a query op
  over the bus, so every worker's list arrives as its per-worker report payload.

Mutations cross the single :class:`~tai42_skeleton.config.service.ConfigService`
pipeline (validate → persist → local reload → broadcast) or, for pure runtime ops,
the shared :func:`~tai42_skeleton.operations._broadcast.broadcast` primitive. Each is
``destructive`` + ``reload_gated`` and its response embeds the per-worker fleet
report as a ``fanout`` summary:

* ``set_mcp_config`` — replace the manifest's MCP section, persist, and reload the fleet.
* ``set_mcp_secret_env`` — write a secret env value AND its ``!ENV ${KEY}`` manifest marker
  together (the combined ``apply_env_and_change`` pipeline), persist, and reload the fleet.
* ``reload_mcp`` — re-probe a single MCP server by title (all workers, or only
  ``targets``). An unknown title is a loud 404.
* ``update_manifest`` — replace the WHOLE persisted manifest and reload the whole
  fleet. Authority-changing (it governs ``api_tools`` + module loading), so it is
  tier-2 (off the default MCP surface, includable).
* ``reload_failed_mcps`` — re-probe every failed MCP server.
* ``deregister_mcp`` — detach a single MCP server's tools by title.

The one response shape every ConfigService writer returns from an
:class:`~tai42_skeleton.config.service.ApplyResult` is built by
:func:`~tai42_skeleton.operations._broadcast.apply_response`.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import Any, cast

from pydantic import BaseModel, Field
from tai42_contract.app import tai42_app
from tai42_kit.utils.data import load_manifest
from tai42_kit.utils.data.env_markers import scan_env_marker_refs

from tai42_skeleton.app.boot_rules import BackendNeedsBusError
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.boundary import registered_env_var_names, x_band_env_keys
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.manifest import AgentsConfig, TaiMCPConfig, ToolsConfig
from tai42_skeleton.operations import BadRequestError, NotFoundError, operation
from tai42_skeleton.operations._broadcast import apply_response, broadcast

# The env var the operator's "treat these env keys as secret" marks live under — a
# comma-separated key-name list backing ``EnvSecretMarksSettings.secret_keys``. The
# secret-env door adds its generated key to this mark so the editor masks it.
_SECRET_MARKS_VAR = "TAI_ENV_SECRET_KEYS"

# A generated secret-env key is a shell identifier; a hint is sanitized to this charset.
_ENV_KEY_START = re.compile(r"[A-Za-z_]")
_NON_ENV_KEY_CHAR = re.compile(r"[^A-Za-z0-9_]+")
# An EXPLICIT key must be a full shell identifier (mirrors file_manager._ENV_KEY_RE, the
# strictest provider) — an odd value is a loud 400 before any write.
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class McpConfigUpdate(BaseModel):
    """A replacement MCP config section — the full list of MCP server entries
    that overwrites the manifest's ``mcp`` list before the reload."""

    mcp: list[TaiMCPConfig]


class McpEntriesAdd(BaseModel):
    """MCP entries to append to the manifest's ``mcp`` section. ``replace``
    lets an entry whose title already exists swap in at its current position;
    without it a title collision is refused."""

    entries: list[TaiMCPConfig]
    replace: bool = False


class ToolsEntriesAdd(BaseModel):
    """Tools entries to append to the manifest's ``tools`` section. ``replace``
    lets an entry whose title already exists swap in at its current position;
    without it a title collision is refused."""

    entries: list[ToolsConfig]
    replace: bool = False


class AgentsEntriesAdd(BaseModel):
    """Agents entries to append to the manifest's ``agents`` section. ``replace``
    lets an entry whose title already exists swap in at its current position;
    without it a title collision is refused."""

    entries: list[AgentsConfig]
    replace: bool = False


class ApiToolsListsUpdate(BaseModel):
    """Names to add to / remove from the manifest ``api_tools`` include and
    exclude lists. Each field is a bare name list; all four empty is refused."""

    include_add: list[str] = Field(default_factory=list)
    include_remove: list[str] = Field(default_factory=list)
    exclude_add: list[str] = Field(default_factory=list)
    exclude_remove: list[str] = Field(default_factory=list)


class McpTargets(BaseModel):
    """An optional fleet fan-out restriction — a ``targets`` list naming the workers
    a single-server MCP action applies to (all workers when omitted)."""

    targets: list[str] | None = None


class FailedMcpsQuery(BaseModel):
    """The failed-MCP listing door's ``?targets=`` fan-out restriction, published as an
    optional array — a repeated ``?targets=`` param a generated client sends once per worker
    (all workers when none is given).

    Spec metadata only — the door parses its query at the HTTP edge."""

    targets: list[str] = Field(
        default_factory=list,
        description="Restrict the listing to these worker names; repeat the parameter per worker, omit for the fleet.",
    )


class ManifestReplace(BaseModel):
    """A full-manifest replacement carrying the manifest TEXT verbatim — the
    PRESERVED view (``!ENV`` markers intact). The server loads it to the preserved
    document and persists it through the pipeline, so it owns resolution and no
    secret bakes to disk. There is no ``targets``: a persisted replacement reaches
    the whole fleet."""

    manifest_text: str


class SetMcpSecretEnv(BaseModel):
    """The combined env+manifest secret op body (``POST /api/mcp-config/secret-env``).

    ``value`` is the raw secret pasted by the operator. The env KEY it is stored under is
    EITHER an explicit ``key`` OR generated from ``key_hint`` — exactly one is given
    (``{value, key | key_hint, manifest_pointer}``). The server writes ``value`` to the env
    store under that key (marked secret) and writes an ``!ENV ${KEY}`` MARKER at
    ``manifest_pointer`` — so the secret lives only in the env store and the manifest carries
    a placeholder. An explicit ``key`` that collides with an existing stored key holding a
    DIFFERENT value is refused with a loud 400 naming the key (never a silent overwrite
    of a live secret). ``manifest_pointer`` is a slash-delimited, no-leading-slash path (e.g.
    ``mcp/0/config/headers/Authorization``) whose HEAD segment MUST be ``mcp`` (the same
    mcp-only authority ``set_mcp_config`` holds). The generated key is NOT returned."""

    value: str
    key: str | None = None
    key_hint: str | None = None
    manifest_pointer: str


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def _preserved_manifest_view() -> dict:
    """The PRESERVED persisted manifest's MCP section + user tools — ``!ENV`` markers
    intact, so NO resolved secret value ever leaves on the wire. A never-written store
    yields an empty view."""
    try:
        preserved = tai42_app.config.config_manager.read_manifest_preserved()
    except FileNotFoundError:
        preserved = {}
    # user_tools may serialize from a set; JSON needs a stable list.
    user_tools = sorted(preserved.get("user_tools", []))
    return {"mcp": preserved.get("mcp", []), "user_tools": user_tools}


@operation(summary="Read the PRESERVED manifest MCP section and user tools", tags=["manifest"])
async def get_manifest() -> dict:
    """Return the MCP section and user tools of the PRESERVED persisted manifest —
    ``!ENV ${KEY}`` markers kept intact, so a secret leaf is its placeholder marker,
    NEVER the plaintext value.

    This door serves ONLY the preserved read: no resolved-view surface exists here, so no
    ``!ENV`` marker is ever materialized onto the wire. McpTab reads
    ``/api/manifest/preserved`` and ManifestTab renders the markers directly.
    """
    return _preserved_manifest_view()


@operation(summary="Read the PRESERVED manifest (markers intact) MCP section and user tools", tags=["manifest"])
async def get_manifest_preserved() -> dict:
    """Return the PRESERVED persisted manifest's MCP section + user tools with every
    ``!ENV ${KEY}`` marker intact (no secret is resolved) — the source the Studio McpTab
    config editor reads so it can round-trip markers instead of baking a resolved secret.
    Same ``{mcp, user_tools}`` view as ``get_manifest`` — both serve the preserved read;
    this explicit ``/preserved`` door names that no-resolve contract in its path."""
    return _preserved_manifest_view()


@operation(summary="List the manifest MCP section's !ENV marker refs (names + set/unset only)", tags=["manifest"])
async def get_mcp_env_refs() -> list[dict[str, Any]]:
    """The ``!ENV ${VAR[:default]}`` markers carried by the manifest's MCP section —
    NAMES and BOOLEANS only, never values.

    Walks the PRESERVED manifest (markers intact) with the shared marker scan and
    returns one row per marker ref, in document order:
    ``{var, pointer, has_default, set}``. ``pointer`` is the RFC 6901 json-pointer of
    the leaf (``/mcp/<i>/config/...``); ``has_default`` is whether the ref carries a
    ``:default``; ``set`` is whether the var is present in ``os.environ`` — the SAME
    effective env the store flows into and the source-marker resolution + dangling
    refusals read, so a var supplied only by the deployment environment shows green,
    never a false red. Works identically for hand-written marker-bearing entries (a
    platform feature, not an mcp-server-kind feature)."""
    section = {"mcp": _preserved_manifest_view()["mcp"]}
    return [
        {
            "var": ref.var,
            "pointer": ref.pointer,
            "has_default": ref.default is not None,
            "set": ref.var in os.environ,
        }
        for ref in scan_env_marker_refs(section)
    ]


@operation(summary="Get the JSON schema for one MCP-config entry", tags=["manifest"])
async def get_mcp_config_schema() -> dict:
    return TaiMCPConfig.model_json_schema()


@operation(summary="Snapshot the live MCP binding status", tags=["manifest"])
async def get_mcp_status() -> dict:
    return tai42_app.admin.live_mcp_status()


@operation(summary="List MCP servers skipped by the viability check", tags=["manifest"], request_model=FailedMcpsQuery)
async def list_failed_mcps(targets: list[str] | None = None) -> Any:
    """List MCP servers skipped due to a failed viability check (server down or
    slow at boot or last reload). Use ``reload_mcp`` to re-attach one once healthy.

    Each entry is ``{"title": <name>, "status": "unavailable"}`` — title plus a
    coarse status only. A query op rides the same fan-out primitive as a mutation:
    every worker's list arrives as its per-worker ``payload`` in the fleet report
    (this worker's list on its own self entry); ``targets`` optionally restricts the
    query to specific workers.
    """

    async def _apply() -> Any:
        # A read, so no reload gate — this worker's failed-MCP list rides its self
        # entry as the payload.
        return tai42_app.admin.list_failed_mcps()

    return await broadcast({"op": "list_failed_mcps"}, targets, _apply)


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------


@operation(
    summary="Replace the MCP config section and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=McpConfigUpdate,
)
async def set_mcp_config(mcp: list[Any]) -> dict:
    # Replace the manifest's ``mcp`` section through the pipeline: the mutator edits
    # the PRESERVED document in place, then ConfigService validates, persists, reloads
    # locally, and broadcasts the reload to the whole fleet. A malformed entry fails
    # validation inside the transaction (nothing persisted) and maps to a loud 400.
    # (No docstring here, so the route description in projection falls back to the
    # operation summary.)
    def mutator(document: dict[str, Any]) -> None:
        document["mcp"] = mcp

    try:
        result = await ConfigService.from_app().apply_change(mutator)
    except BackendNeedsBusError as exc:
        # The invariant is a RuntimeError (a boot-time refusal must still crash loudly),
        # so the mutate-time path maps it explicitly to a loud, actionable 400 naming
        # TAI_BUS_REDIS_URL rather than letting it escape as a 500.
        raise BadRequestError(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestError(f"invalid mcp config: {exc}") from exc
    return apply_response(result)


# ---------------------------------------------------------------------------
# Per-entry manifest-section add/remove (mcp / tools / agents)
# ---------------------------------------------------------------------------
#
# One add-mutator and one remove-mutator, parameterized on the section key, back the
# six thin ops below — the sections share title-keyed identity and the same collision /
# membership contract. Deep per-entry validation stays with the pipeline
# (``_validate_manifest``), exactly as ``set_mcp_config``.


def _merged_entries(current: list[Any], entries: list[Any], replace: bool) -> list[Any]:
    """The new section list from ``current`` + incoming ``entries``. Every entry must be
    a dict carrying a non-empty ``title`` string (else a ``ValueError`` naming the
    position); duplicate incoming titles are refused. A title already present is refused
    unless ``replace`` swaps the entry in at its current index; non-colliding entries
    append in given order. Pure / re-runnable: builds a fresh list from the arguments."""
    incoming_titles: list[str] = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"entry at position {position} must be a mapping carrying a 'title'")
        title = entry.get("title")
        if not isinstance(title, str) or not title:
            raise ValueError(f"entry at position {position} must carry a non-empty 'title' string")
        incoming_titles.append(title)
    duplicates = sorted({t for t in incoming_titles if incoming_titles.count(t) > 1})
    if duplicates:
        raise ValueError(f"duplicate titles within entries: {duplicates}")

    current_titles = [e.get("title") if isinstance(e, dict) else None for e in current]
    by_title = dict(zip(incoming_titles, entries, strict=True))
    collisions = sorted(set(incoming_titles) & {t for t in current_titles if t is not None})
    if collisions and not replace:
        raise ValueError(f"entries already present (use replace): {collisions}")

    collided = set(collisions)
    kept = zip(current, current_titles, strict=True)
    result = [by_title[title] if title in collided else entry for entry, title in kept]
    result.extend(entry for title, entry in zip(incoming_titles, entries, strict=True) if title not in collided)
    return result


def _without_entry(current: list[Any], title: str) -> list[Any]:
    """``current`` minus the entry whose ``title`` matches; a missing title is a
    ``LookupError`` (the mutator aborts the transaction). Pure / re-runnable."""
    result = [e for e in current if not (isinstance(e, dict) and e.get("title") == title)]
    if len(result) == len(current):
        raise LookupError(title)
    return result


async def _apply_entries_add(section: str, entries: list[Any], replace: bool) -> dict:
    # Empty ``entries`` is refused INSIDE the try (op-level, so the projection path is
    # refused identically) — a guard above the try would escape as a 500.
    try:
        if not entries:
            raise ValueError("entries must be a non-empty list")

        def mutator(document: dict[str, Any]) -> None:
            document[section] = _merged_entries(document.get(section) or [], entries, replace)

        result = await ConfigService.from_app().apply_change(mutator)
    except BackendNeedsBusError as exc:
        raise BadRequestError(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestError(f"invalid {section} config: {exc}") from exc
    return apply_response(result)


async def _apply_entry_remove(section: str, title: str) -> dict:
    try:

        def mutator(document: dict[str, Any]) -> None:
            document[section] = _without_entry(document.get(section) or [], title)

        result = await ConfigService.from_app().apply_change(mutator)
    except BackendNeedsBusError as exc:
        raise BadRequestError(str(exc)) from exc
    except LookupError as exc:
        raise NotFoundError(f"unknown {section} entry title: {title!r}") from exc
    except ValueError as exc:
        raise BadRequestError(f"invalid {section} config: {exc}") from exc
    return apply_response(result)


@operation(
    summary="Add or replace MCP config entries and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=McpEntriesAdd,
)
async def add_mcp_entries(entries: list[Any], replace: bool = False) -> dict:
    return await _apply_entries_add("mcp", entries, replace)


@operation(
    summary="Remove one MCP config entry by title and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, NotFoundError],
)
async def remove_mcp_entry(title: str) -> dict:
    return await _apply_entry_remove("mcp", title)


@operation(
    summary="Add or replace tools config entries and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=ToolsEntriesAdd,
)
async def add_tools_entries(entries: list[Any], replace: bool = False) -> dict:
    return await _apply_entries_add("tools", entries, replace)


@operation(
    summary="Remove one tools config entry by title and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, NotFoundError],
)
async def remove_tools_entry(title: str) -> dict:
    return await _apply_entry_remove("tools", title)


@operation(
    summary="Add or replace agents config entries and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=AgentsEntriesAdd,
)
async def add_agents_entries(entries: list[Any], replace: bool = False) -> dict:
    return await _apply_entries_add("agents", entries, replace)


@operation(
    summary="Remove one agents config entry by title and hot-reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, NotFoundError],
)
async def remove_agents_entry(title: str) -> dict:
    return await _apply_entry_remove("agents", title)


def _edit_name_list(current: list[Any], add: list[str], remove: list[str], field: str) -> list[str]:
    """The edited ``api_tools`` include/exclude list. A name in ``add`` already present is
    refused (``ValueError`` naming it); a name in ``remove`` absent is refused
    (``LookupError`` naming it). Order-stable: kept names first, additions appended. Pure
    / re-runnable: builds a fresh list from the arguments."""
    names = [str(n) for n in current]
    present = set(names)
    already = sorted(n for n in add if n in present)
    if already:
        raise ValueError(f"api_tools {field}: already present: {already}")
    absent = sorted(n for n in remove if n not in present)
    if absent:
        raise LookupError(f"api_tools {field}: not present: {absent}")
    dropped = set(remove)
    return [n for n in names if n not in dropped] + list(add)


@operation(
    summary="Add/remove names on the api_tools include/exclude lists and hot-reload",
    tags=["manifest"],
    authority_changing=True,
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError, NotFoundError],
    request_model=ApiToolsListsUpdate,
)
async def update_api_tools(
    include_add: list[str] | None = None,
    include_remove: list[str] | None = None,
    exclude_add: list[str] | None = None,
    exclude_remove: list[str] | None = None,
) -> dict:
    include_add = include_add or []
    include_remove = include_remove or []
    exclude_add = exclude_add or []
    exclude_remove = exclude_remove or []
    try:
        if not (include_add or include_remove or exclude_add or exclude_remove):
            raise ValueError("nothing to change")

        def mutator(document: dict[str, Any]) -> None:
            api_tools = document.get("api_tools")
            if not isinstance(api_tools, dict):
                api_tools = {}
                document["api_tools"] = api_tools
            included = _edit_name_list(api_tools.get("include") or [], include_add, include_remove, "include")
            excluded = _edit_name_list(api_tools.get("exclude") or [], exclude_add, exclude_remove, "exclude")
            api_tools["include"] = included
            api_tools["exclude"] = excluded

        result = await ConfigService.from_app().apply_change(mutator)
    except BackendNeedsBusError as exc:
        raise BadRequestError(str(exc)) from exc
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestError(f"invalid api_tools config: {exc}") from exc
    return apply_response(result)


def _parse_manifest_pointer(pointer: str) -> list[str]:
    """Split a slash-delimited, no-leading-slash manifest pointer into segments and
    enforce the mcp-only authority: the HEAD segment MUST be ``mcp`` (a loud 400 like
    ``set_mcp_config``'s), and the pointer must address a leaf UNDER ``mcp`` (at least
    one segment past the head). A leading slash, an empty segment, or a wrong head is a
    loud 400."""
    segments = pointer.split("/")
    if not segments or segments[0] != "mcp":
        raise BadRequestError(
            f"manifest_pointer head segment must be 'mcp' (slash-delimited, no leading slash); got {pointer!r}"
        )
    if len(segments) < 2 or any(seg == "" for seg in segments):
        raise BadRequestError(
            f"manifest_pointer must address a leaf under 'mcp' with no empty segments; got {pointer!r}"
        )
    return segments


def _derive_secret_env_key(key_hint: str, taken: frozenset[str]) -> str:
    """Derive a valid, unique env KEY from ``key_hint`` (a key_hint-based name).

    The hint is uppercased and reduced to the shell-identifier charset; an empty or
    non-identifier result falls back to ``SECRET``. The name is then made unique against
    ``taken`` (stored env keys and the X band) by appending ``_2``, ``_3``, … — so a
    generated key never clobbers an existing key nor collides with a deployment X-band
    name. The value is NEVER derived from the secret and is never returned to the caller."""
    base = _NON_ENV_KEY_CHAR.sub("_", key_hint).strip("_").upper()
    if not base or not _ENV_KEY_START.match(base):
        base = f"SECRET_{base}".rstrip("_") if base else "SECRET"
    candidate = base
    suffix = 2
    while candidate in taken:
        candidate = f"{base}_{suffix}"
        suffix += 1
    return candidate


def _resolve_secret_env_key(explicit: str | None, hint: str | None, value: str, stored: dict[str, str]) -> str:
    """Resolve the env KEY a secret is stored under: exactly one of an EXPLICIT ``explicit``
    key or a ``hint`` to generate from (``key | key_hint``).

    An explicit key is validated to the shell-identifier charset (an odd value is a loud
    ``ValueError`` → 400) and REFUSED if it collides with an existing stored key holding a
    DIFFERENT value (never a silent overwrite of a live secret; an identical value is an
    idempotent re-send, no collision). A generated key is made unique against the stored keys,
    the X band, AND every registered settings ``env_var``, so it never clobbers a stored key
    nor SHADOWS a registered var. Raises ``ValueError`` (the op maps it to a 400)."""
    if (explicit is None) == (hint is None):
        raise ValueError("provide exactly one of 'key' (explicit) or 'key_hint' (to generate from)")
    if explicit is not None:
        if not _ENV_KEY_RE.fullmatch(explicit):
            raise ValueError(f"invalid env key {explicit!r}: must match {_ENV_KEY_RE.pattern!r}")
        if explicit in stored and stored[explicit] != value:
            raise ValueError(
                f"explicit key {explicit!r} collides with an existing stored env key holding a "
                "different value — refusing to silently overwrite a live secret"
            )
        return explicit
    taken = frozenset(stored) | x_band_env_keys() | registered_env_var_names()
    return _derive_secret_env_key(cast("str", hint), taken)


def _set_marker_at_pointer(document: dict[str, Any], segments: list[str], marker: str) -> None:
    """Set ``document`` at the location named by ``segments`` (head ``mcp``) to ``marker``.

    A numeric segment indexes a list (in range, else a loud ``ValueError``); a non-numeric
    segment keys a mapping — a missing mapping key is created (as a list when the next
    segment is numeric, else a mapping) so a NEW leaf under an existing MCP entry can be
    written. Pure / re-runnable: it only edits ``document`` (the k8s 409-replay contract).
    A path that traverses a non-container, or a numeric segment out of range, is a
    ``ValueError`` the door maps to a 400."""
    node: Any = document
    for depth, seg in enumerate(segments[:-1]):
        nxt = segments[depth + 1]
        if isinstance(node, list):
            node = node[_pointer_index(node, seg, segments)]
        elif isinstance(node, dict):
            if seg not in node:
                node[seg] = [] if nxt.isdigit() else {}
            node = node[seg]
        else:
            raise ValueError(f"manifest_pointer traverses a non-container at {seg!r} in {'/'.join(segments)!r}")
    last = segments[-1]
    if isinstance(node, list):
        node[_pointer_index(node, last, segments)] = marker
    elif isinstance(node, dict):
        node[last] = marker
    else:
        raise ValueError(f"manifest_pointer traverses a non-container at {last!r} in {'/'.join(segments)!r}")


def _pointer_index(node: list[Any], segment: str, segments: list[str]) -> int:
    """A numeric list-index segment resolved against ``node``; a non-numeric or
    out-of-range segment is a loud ``ValueError`` (→ 400)."""
    if not segment.isdigit():
        raise ValueError(f"manifest_pointer segment {segment!r} must be a list index in {'/'.join(segments)!r}")
    index = int(segment)
    if index >= len(node):
        raise ValueError(f"manifest_pointer list index {index} out of range in {'/'.join(segments)!r}")
    return index


@operation(
    summary="Write a secret env value and its manifest !ENV marker together, then reload",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[BadRequestError],
    request_model=SetMcpSecretEnv,
)
async def set_mcp_secret_env(
    value: str, manifest_pointer: str, key: str | None = None, key_hint: str | None = None
) -> dict:
    """Store a pasted secret as an env value and reference it from the manifest by a marker.

    The env KEY is EITHER an explicit ``key`` OR generated from ``key_hint`` — exactly one
    (``key | key_hint``). Writes the secret ``value`` to the env store under that key AND
    marks the key secret (adding it to ``TAI_ENV_SECRET_KEYS``), and writes an ``!ENV ${KEY}``
    MARKER at ``manifest_pointer`` — all through the combined
    ``ConfigService.apply_env_and_change`` pipeline so the env write and the manifest mutate
    stay consistent. Partial-failure: a manifest-persist failure after the env write does
    NO rollback — the env write stands as an inert, re-runnable orphan and the op raises
    loudly. An explicit ``key`` colliding with an existing stored key holding a DIFFERENT value
    is a loud 400 naming the key; a generated key never shadows a registered settings
    ``env_var``. The pointer's HEAD segment MUST be ``mcp`` (loud 400 otherwise). The response
    is the ``reloadConfigResult`` shape; the resolved key is NEVER returned. A dangling
    ``!ENV`` / X-band refusal surfaces as a loud 400 naming the key (the shared boundary
    validator, same as ``POST /api/mcp-config``)."""
    segments = _parse_manifest_pointer(manifest_pointer)  # loud 400 on a non-mcp head

    service = ConfigService.from_app()

    async def prepare(stored: dict[str, str]) -> tuple[dict[str, str], Callable[[dict[str, Any]], None]]:
        # Runs INSIDE the ConfigService env-write lock, against the stored env read once
        # under that lock — so the key resolution and the secret-marks append are read-then-
        # write against a snapshot no concurrent combined op can clobber.
        #
        # Resolve the key: an explicit key (collision-refused) or generated from the hint
        # (unique against stored keys, the X band, and every registered env_var). A bad key is
        # a ValueError the op maps to a 400 below (raised before any write).
        key_resolved = _resolve_secret_env_key(key, key_hint, value, stored)

        # Mark the new key secret: APPEND to the current marks read from the STORED env —
        # never the settings cache, which is stale until a reload and would clobber a mark a
        # prior op just added. Read→append→fold into the same write_env merge, order-stable.
        existing_marks = [m.strip() for m in stored.get(_SECRET_MARKS_VAR, "").split(",") if m.strip()]
        marks = list(dict.fromkeys([*existing_marks, key_resolved]))
        changes = {key_resolved: value, _SECRET_MARKS_VAR: ",".join(marks)}
        marker = f"!ENV ${{{key_resolved}}}"

        def mutator(document: dict[str, Any]) -> None:
            _set_marker_at_pointer(document, segments, marker)

        return changes, mutator

    try:
        result = await service.apply_env_and_change(prepare, manifest_pointer=manifest_pointer)
    except BackendNeedsBusError as exc:
        raise BadRequestError(str(exc)) from exc
    except ValueError as exc:
        # A bad/colliding key from _resolve_secret_env_key (raised inside prepare, before any
        # write) or a boundary refusal (X-band / dangling marker) — a loud 400 either way.
        raise BadRequestError(str(exc)) from exc
    return apply_response(result)


@operation(
    summary="Reload a single MCP server by title",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[NotFoundError],
    request_model=McpTargets,
)
async def reload_mcp(title: str, targets: list[str] | None = None) -> Any:
    # Re-probe a single MCP server by title (unknown title → loud 404), applied on
    # this worker through the gate and broadcast to the fleet (all workers, or only
    # ``targets``); the response embeds the per-worker fleet report. A pure runtime
    # op: if the local re-probe raises, nothing is broadcast. (No docstring here, so
    # the route description in projection falls back to the operation summary.)
    live = tai42_app.admin.live_manifest
    titles = {entry.get("title") for entry in live.get("mcp", [])}
    if title not in titles:
        raise NotFoundError(f"unknown mcp title: {title!r}")
    return await broadcast(
        {"op": "reload_mcp", "title": title},
        targets,
        lambda: reload_gate.run(lambda: tai42_app.admin.reload_mcp(title)),
    )


@operation(
    summary="Replace the whole manifest, persist, and reload the fleet",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    authority_changing=True,
    errors=[BadRequestError],
    request_model=ManifestReplace,
)
async def update_manifest(manifest_text: str) -> Any:
    """Replace the WHOLE manifest fleet-wide and persist it.

    The posted ``manifest_text`` is the PRESERVED view (``!ENV`` markers intact); the
    server loads it to the preserved document and pushes it through the pipeline —
    validate the RESOLVED projection, persist verbatim (no secret bakes to disk),
    reload locally, and broadcast the reload so every worker re-reads the persisted
    store. The response embeds the per-worker fleet report as its ``fanout`` summary.
    A persisted replacement reaches the whole fleet, so there is no ``targets``.

    Authority-changing — the manifest governs ``api_tools`` + module loading — so it
    is off the default MCP surface (tier 2), projectable via an explicit
    ``api_tools.include``.
    """
    try:
        document = cast("dict[str, Any]", load_manifest(manifest_text))
    except Exception as exc:
        raise BadRequestError(f"invalid manifest: {exc}") from exc
    try:
        result = await ConfigService.from_app().apply_replace(document)
    except BackendNeedsBusError as exc:
        # The invariant is a RuntimeError (a boot-time refusal must still crash loudly),
        # so the mutate-time path maps it explicitly to a loud, actionable 400 naming
        # TAI_BUS_REDIS_URL rather than letting it escape as a 500.
        raise BadRequestError(str(exc)) from exc
    except ValueError as exc:
        raise BadRequestError(f"invalid manifest: {exc}") from exc
    return apply_response(result)


@operation(
    summary="Re-probe every failed MCP server",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    request_model=McpTargets,
)
async def reload_failed_mcps(targets: list[str] | None = None) -> Any:
    """Re-probe every MCP server currently in the failed list and attach the ones now
    viable. Applied on this worker through the gate and broadcast to the fleet (all
    workers, or only ``targets``); the response embeds the per-worker fleet report.
    """
    # Run the heavy sync re-probe pass on a worker thread through the gate.
    return await broadcast(
        {"op": "reload_failed_mcps"},
        targets,
        lambda: reload_gate.run(tai42_app.admin.reload_failed_mcps),
    )


@operation(
    summary="Detach a single MCP server's tools by title",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    request_model=McpTargets,
)
async def deregister_mcp(title: str, targets: list[str] | None = None) -> Any:
    """Detach a single MCP server's tools (by manifest title) without touching the
    other servers — the removal counterpart of ``reload_mcp``. Applied on this worker
    through the gate and broadcast to the fleet (all workers, or only ``targets``);
    the response embeds the per-worker fleet report.
    """
    # Run the heavy sync detach on a worker thread through the gate.
    return await broadcast(
        {"op": "deregister_mcp", "title": title},
        targets,
        lambda: reload_gate.run(lambda: tai42_app.admin.deregister_mcp(title)),
    )
