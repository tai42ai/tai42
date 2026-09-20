"""Manifest + MCP-status read doors and the preserved-manifest view helper."""

from __future__ import annotations

import os
from typing import Any

from tai42_contract.app import tai42_app
from tai42_contract.app.responses import OpaqueJson
from tai42_kit.utils.data.env_markers import scan_env_marker_refs

import tai42_skeleton.operations.manifest as _pkg
from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.manifest import TaiMCPConfig
from tai42_skeleton.operations import operation
from tai42_skeleton.operations._broadcast import broadcast
from tai42_skeleton.operations.response_models_group_a import (
    McpEnvRefList,
    McpStatusSnapshot,
    PreservedManifestView,
)

from .models import FailedMcpsQuery


def _preserved_manifest_view() -> dict:
    """The PRESERVED persisted manifest's MCP section + user tools, ``!ENV`` markers intact.

    No resolved secret value ever leaves on the wire. A never-written store yields an empty view.
    """
    try:
        preserved = tai42_app.config.config_manager.read_manifest_preserved()
    except FileNotFoundError:
        preserved = {}
    # user_tools may serialize from a set; JSON needs a stable list.
    user_tools = sorted(preserved.get("user_tools", []))
    return {"mcp": preserved.get("mcp", []), "user_tools": user_tools}


@operation(
    summary="Read the PRESERVED manifest MCP section and user tools",
    tags=["manifest"],
    response_model=PreservedManifestView,
)
async def get_manifest() -> dict:
    """Return the MCP section and user tools of the PRESERVED persisted manifest.

    ``!ENV ${KEY}`` markers are kept intact, so a secret leaf is its placeholder marker, NEVER the
    plaintext value.

    This door serves ONLY the preserved read: no resolved-view surface exists here, so no
    ``!ENV`` marker is ever materialized onto the wire. McpTab reads
    ``/api/manifest/preserved`` and ManifestTab renders the markers directly.
    """
    return _pkg._preserved_manifest_view()


@operation(
    summary="Read the PRESERVED manifest (markers intact) MCP section and user tools",
    tags=["manifest"],
    response_model=PreservedManifestView,
)
async def get_manifest_preserved() -> dict:
    """Return the PRESERVED persisted manifest's MCP section + user tools, every ``!ENV`` marker intact.

    No secret is resolved — the source the Studio McpTab config editor reads so it can round-trip
    markers instead of baking a resolved secret. Same ``{mcp, user_tools}`` view as
    ``get_manifest`` — both serve the preserved read; this explicit ``/preserved`` door names that
    no-resolve contract in its path.
    """
    return _pkg._preserved_manifest_view()


@operation(
    summary="List the manifest MCP section's !ENV marker refs (names + set/unset only)",
    tags=["manifest"],
    response_model=McpEnvRefList,
)
async def get_mcp_env_refs() -> list[dict[str, Any]]:
    """The ``!ENV ${VAR[:default]}`` markers carried by the manifest's MCP section.

    NAMES and BOOLEANS only, never values.

    Walks the PRESERVED manifest (markers intact) with the shared marker scan and
    returns one row per marker ref, in document order:
    ``{var, pointer, has_default, set}``. ``pointer`` is the RFC 6901 json-pointer of
    the leaf (``/mcp/<i>/config/...``); ``has_default`` is whether the ref carries a
    ``:default``; ``set`` is whether the var is present in ``os.environ`` — the SAME
    effective env the store flows into and the source-marker resolution + dangling
    refusals read, so a var supplied only by the deployment environment shows green,
    never a false red. Works identically for hand-written marker-bearing entries (a
    platform feature, not an mcp-server-kind feature).
    """
    section = {"mcp": _pkg._preserved_manifest_view()["mcp"]}
    return [
        {
            "var": ref.var,
            "pointer": ref.pointer,
            "has_default": ref.default is not None,
            "set": ref.var in os.environ,
        }
        for ref in scan_env_marker_refs(section)
    ]


@operation(summary="Get the JSON schema for one MCP-config entry", tags=["manifest"], response_model=OpaqueJson)
async def get_mcp_config_schema() -> dict:
    """The JSON schema for one MCP-config entry."""
    return TaiMCPConfig.model_json_schema()


@operation(summary="Snapshot the live MCP binding status", tags=["manifest"], response_model=McpStatusSnapshot)
async def get_mcp_status() -> dict:
    """A snapshot of the live MCP binding status."""
    return tai42_app.admin.live_mcp_status()


@operation(
    summary="List MCP servers skipped by the viability check",
    tags=["manifest"],
    request_model=FailedMcpsQuery,
    response_model=FleetResult,
)
async def list_failed_mcps(targets: list[str] | None = None) -> Any:
    """List MCP servers skipped due to a failed viability check (server down or slow).

    Skipped at boot or last reload. Use ``reload_mcp`` to re-attach one once healthy.

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
