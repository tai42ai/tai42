"""Pure runtime fleet MCP ops: reload one/all, replace the manifest, deregister one."""

from __future__ import annotations

from typing import Any, cast

from tai42_contract.app import tai42_app
from tai42_contract.app.responses import ApplyResponse
from tai42_kit.utils.data import load_manifest

from tai42_skeleton.app.boot_rules import translate_config_invariants
from tai42_skeleton.app.bus import FleetResult
from tai42_skeleton.app.bus.ops import DeregisterMcp, ReloadFailedMcps, ReloadMcp
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.service import ConfigService
from tai42_skeleton.operations import BadRequestError, ConflictError, NotFoundError, UpstreamError, operation
from tai42_skeleton.operations._broadcast import apply_response, broadcast, translate_orphan_env_write
from tai42_skeleton.operations.response_models.manifest import McpAdvertisedTools
from tai42_skeleton.tools.mcp_health import redact_probe_error

from .models import ManifestReplace, McpTargets


@operation(
    summary="Reload a single MCP server by title",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    errors=[NotFoundError],
    request_model=McpTargets,
    response_model=FleetResult,
)
async def reload_mcp(title: str, targets: list[str] | None = None) -> Any:
    """Re-probe a single MCP server by ``title`` (unknown title → loud 404) and reattach it.

    Applied on this worker through the gate and broadcast to the fleet (all workers, or only
    ``targets``); the response embeds the per-worker fleet report. If the local re-probe raises,
    nothing is broadcast.
    """
    live = tai42_app.admin.live_manifest
    titles = {entry.get("title") for entry in live.get("mcp", [])}
    if title not in titles:
        raise NotFoundError(f"unknown mcp title: {title!r}")
    return await broadcast(
        ReloadMcp(op="reload_mcp", title=title),
        targets,
        lambda: reload_gate.run(lambda: tai42_app.admin.reload_mcp(title), reimports=False),
    )


@operation(
    summary="Probe an MCP server for its advertised tools",
    tags=["manifest"],
    errors=[NotFoundError, UpstreamError],
    response_model=McpAdvertisedTools,
)
async def list_mcp_advertised_tools(title: str) -> dict[str, Any]:
    """Every tool the live MCP server ``title`` advertises, before the selection, with the entry's lists.

    ``unadvertised`` names the entries of the set list (``include`` when non-empty, else ``exclude``)
    that no advertised tool carries, in stored order. Unknown title → 404; a server that cannot be
    listed → 502.
    """
    live = tai42_app.admin.live_manifest
    entry = next((row for row in live.get("mcp", []) if row.get("title") == title), None)
    if entry is None:
        raise NotFoundError(f"unknown mcp title {title!r}")
    try:
        tools = await tai42_app.admin.advertised_mcp_tools(title)
    except LookupError as exc:
        raise NotFoundError(str(exc)) from exc
    except Exception as exc:
        raise UpstreamError(f"mcp server {title!r} did not list its tools: {redact_probe_error(str(exc))}") from exc
    include = list(entry.get("include") or [])
    exclude = list(entry.get("exclude") or [])
    advertised = {row["name"] for row in tools}
    return {
        "title": title,
        "include": include,
        "exclude": exclude,
        "tools": tools,
        "unadvertised": [name for name in (include or exclude) if name not in advertised],
    }


@operation(
    summary="Replace the whole manifest, persist, and reload the fleet",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    authority_changing=True,
    errors=[BadRequestError, ConflictError, UpstreamError],
    request_model=ManifestReplace,
    response_model=ApplyResponse,
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
    with translate_orphan_env_write(), translate_config_invariants(BadRequestError):
        try:
            result = await ConfigService.from_app().apply_replace(document)
        except ValueError as exc:
            raise BadRequestError(f"invalid manifest: {exc}") from exc
        return apply_response(result)


@operation(
    summary="Re-probe every failed MCP server",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    request_model=McpTargets,
    response_model=FleetResult,
)
async def reload_failed_mcps(targets: list[str] | None = None) -> Any:
    """Re-probe every MCP server currently in the failed list and attach the ones now viable.

    Applied on this worker through the gate and broadcast to the fleet (all workers, or only
    ``targets``); the response embeds the per-worker fleet report.
    """
    # Run the heavy sync re-probe pass on a worker thread through the gate.
    return await broadcast(
        ReloadFailedMcps(op="reload_failed_mcps"),
        targets,
        lambda: reload_gate.run(tai42_app.admin.reload_failed_mcps, reimports=False),
    )


@operation(
    summary="Detach a single MCP server's tools by title",
    tags=["manifest"],
    destructive=True,
    reload_gated=True,
    request_model=McpTargets,
    response_model=FleetResult,
)
async def deregister_mcp(title: str, targets: list[str] | None = None) -> Any:
    """Detach a single MCP server's tools (by manifest title) without touching the other servers.

    The removal counterpart of ``reload_mcp``. Applied on this worker through the gate and
    broadcast to the fleet (all workers, or only ``targets``); the response embeds the per-worker
    fleet report.
    """
    # Run the heavy sync detach on a worker thread through the gate.
    return await broadcast(
        DeregisterMcp(op="deregister_mcp", title=title),
        targets,
        lambda: reload_gate.run(lambda: tai42_app.admin.deregister_mcp(title), reimports=False),
    )
