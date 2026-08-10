"""``tai mcp`` — inspect and configure mounted MCP servers.

Thin wrappers over the ``/api/mcp-config*`` and ``/api/mcp-status*`` routes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from tai42_cli.commands._common import app_context, covers, emit_result

app = typer.Typer(
    name="mcp",
    help="Inspect and configure mounted MCP servers.",
    no_args_is_help=True,
)


def _load_add_entries(file: Path) -> list[Any]:
    """Read an ``add``-file's entries: ONE entry object, a bare JSON array, or an
    object with an ``"entries"`` list. An object is a single entry unless it has an
    ``"entries"`` key, whose value must then be a list."""
    try:
        parsed = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"file must be valid JSON: {exc}", param_hint="--file") from exc
    if isinstance(parsed, dict):
        if "entries" not in parsed:
            return [parsed]
        entries = parsed["entries"]
        if isinstance(entries, list):
            return entries
        # an "entries" key that is not a list is a malformed wrapper, not a single entry
    if isinstance(parsed, list):
        return parsed
    raise typer.BadParameter(
        "file must be one entry object, a JSON array of entries, or an object with an 'entries' list",
        param_hint="--file",
    )


@app.command("status")
@covers(("GET", "/api/mcp-status"))
def mcp_status(ctx: typer.Context) -> None:
    """Snapshot the live MCP binding status.

    Example: ``tai mcp status``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/mcp-status")
    emit_result(ctx_obj, data)


@app.command("schema")
@covers(("GET", "/api/mcp-config/schema"))
def mcp_config_schema(ctx: typer.Context) -> None:
    """Get the JSON schema for one MCP-config entry.

    Example: ``tai mcp schema``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/mcp-config/schema")
    emit_result(ctx_obj, data)


@app.command("set")
@covers(("POST", "/api/mcp-config"))
def set_mcp_config(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Option(
            "--file", exists=True, dir_okay=False, readable=True, help="A JSON file with the full 'mcp' list."
        ),
    ],
) -> None:
    """Replace the MCP config section from a JSON file and hot-reload.

    The file is the full replacement MCP list, either a bare JSON array or an
    object carrying an ``"mcp"`` list.

    Example: ``tai mcp set --file mcp.json``
    """
    ctx_obj = app_context(ctx)
    try:
        parsed = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"file must be valid JSON: {exc}", param_hint="--file") from exc
    if isinstance(parsed, dict) and "mcp" in parsed:
        mcp = parsed["mcp"]
    elif isinstance(parsed, list):
        mcp = parsed
    else:
        raise typer.BadParameter("file must be a JSON list or an object with an 'mcp' list", param_hint="--file")
    with ctx_obj.client() as client:
        data = client.post("/api/mcp-config", json={"mcp": mcp})
    emit_result(ctx_obj, data)


@app.command("add")
@covers(("POST", "/api/mcp-config/entries"))
def add_mcp_entries(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Option(
            "--file", exists=True, dir_okay=False, readable=True, help="A JSON file with the MCP entries to add."
        ),
    ],
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Replace entries whose title already exists (in place) instead of refusing."),
    ] = False,
) -> None:
    """Add MCP config entries to the manifest and hot-reload.

    The file is ONE entry object, a bare JSON array of entries, or an object
    carrying an ``"entries"`` list. Without ``--replace`` an entry whose title
    already exists is refused.

    Example: ``tai mcp add --file entries.json``
    """
    ctx_obj = app_context(ctx)
    entries = _load_add_entries(file)
    with ctx_obj.client() as client:
        data = client.post("/api/mcp-config/entries", json={"entries": entries, "replace": replace})
    emit_result(ctx_obj, data)


@app.command("remove")
@covers(("DELETE", "/api/mcp-config/entries/{title}"))
def remove_mcp_entry(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="MCP server title.")],
) -> None:
    """Remove one MCP config entry by title and hot-reload.

    Persisted removal (see `deregister` for runtime-only detach).

    Example: ``tai mcp remove my-server``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/mcp-config/entries/{title}")
    emit_result(ctx_obj, data)


def _targets_body(target: list[str] | None) -> dict[str, list[str] | None]:
    return {"targets": list(target) if target else None}


@app.command("reload")
@covers(("POST", "/api/mcp-status/{title}/reload"))
def reload_mcp(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="MCP server title.")],
    target: Annotated[
        list[str] | None, typer.Option("--target", help="A worker to restrict the fan-out to (repeatable).")
    ] = None,
) -> None:
    """Reload a single MCP server by title.

    Without ``--target`` the response is the single-worker status; with one or more
    the confirmed fleet fan-out is awaited and per-worker results are returned.

    Example: ``tai mcp reload my-server``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/mcp-status/{title}/reload", json=_targets_body(target))
    emit_result(ctx_obj, data)


@app.command("failed")
@covers(("GET", "/api/mcp-status/failed"))
def list_failed_mcps(
    ctx: typer.Context,
    target: Annotated[
        list[str] | None, typer.Option("--target", help="A worker to restrict the listing to (repeatable).")
    ] = None,
) -> None:
    """List the MCP servers skipped by the viability check (down/slow at boot or reload).

    Example: ``tai mcp failed``
    """
    ctx_obj = app_context(ctx)
    params = {"targets": list(target)} if target else None
    with ctx_obj.client() as client:
        data = client.get("/api/mcp-status/failed", params=params)
    emit_result(ctx_obj, data)


@app.command("reload-failed")
@covers(("POST", "/api/mcp-status/reload-failed"))
def reload_failed_mcps(
    ctx: typer.Context,
    target: Annotated[
        list[str] | None, typer.Option("--target", help="A worker to restrict the fan-out to (repeatable).")
    ] = None,
) -> None:
    """Re-probe every failed MCP server and attach the ones now viable.

    Example: ``tai mcp reload-failed``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post("/api/mcp-status/reload-failed", json=_targets_body(target))
    emit_result(ctx_obj, data)


@app.command("deregister")
@covers(("POST", "/api/mcp-status/{title}/deregister"))
def deregister_mcp(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="MCP server title.")],
    target: Annotated[
        list[str] | None, typer.Option("--target", help="A worker to restrict the fan-out to (repeatable).")
    ] = None,
) -> None:
    """Detach a single MCP server's tools by title (the removal counterpart of reload).

    Runtime-only detach; for persisted removal see `remove`.

    Example: ``tai mcp deregister my-server``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/mcp-status/{title}/deregister", json=_targets_body(target))
    emit_result(ctx_obj, data)
