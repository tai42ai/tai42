"""``tai manifest`` — inspect and edit the server manifest.

``show`` reads the live manifest over ``/api/manifest``, ``plugins`` lists the
installed studio plugins over ``/api/plugins``, and ``replace`` installs a manifest
file over ``/api/manifest/replace``. The ``tools-add``/``tools-remove``,
``agents-add``/``agents-remove``, and ``api-tools`` commands edit single manifest
entries in place over the ``/api/tools-config``, ``/api/agents-config``, and
``/api/api-tools`` routes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_records,
    emit_result,
)

app = typer.Typer(
    name="manifest",
    help="Inspect the server manifest.",
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


@app.command("show")
@covers(("GET", "/api/manifest"))
def show_manifest(ctx: typer.Context) -> None:
    """Show the live manifest's MCP section and user tools.

    Example: ``tai manifest show``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/manifest")
    emit_result(ctx_obj, data)


@app.command("plugins")
@covers(("GET", "/api/plugins"))
def list_plugins(ctx: typer.Context) -> None:
    """List the installed studio plugins declared by the manifest.

    Example: ``tai manifest plugins``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/plugins")
    emit_records(ctx_obj, data, ["name"])


@app.command("replace")
@covers(("POST", "/api/manifest/replace"))
def replace_manifest(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Option("--file", exists=True, dir_okay=False, readable=True, help="A manifest.yml file to install."),
    ],
) -> None:
    """Replace the WHOLE persisted manifest from a file and reload the fleet.

    Posts the manifest TEXT verbatim — ``!ENV`` markers are left INTACT so the server
    resolves them (the persist-through replace keeps the preserved view, never baking
    a resolved secret to disk). The persisted change reaches the whole fleet.

    Example: ``tai manifest replace --file config/manifest.yml``
    """
    ctx_obj = app_context(ctx)
    manifest_text = file.read_text()
    with ctx_obj.client() as client:
        data = client.post("/api/manifest/replace", json={"manifest_text": manifest_text})
    emit_result(ctx_obj, data)


@app.command("tools-add")
@covers(("POST", "/api/tools-config/entries"))
def add_tools_entries(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Option(
            "--file", exists=True, dir_okay=False, readable=True, help="A JSON file with the tools entries to add."
        ),
    ],
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Replace entries whose title already exists (in place) instead of refusing."),
    ] = False,
) -> None:
    """Add tools config entries to the manifest and hot-reload.

    The file is ONE entry object, a bare JSON array of entries, or an object
    carrying an ``"entries"`` list. Without ``--replace`` an entry whose title
    already exists is refused.

    Example: ``tai manifest tools-add --file entries.json``
    """
    ctx_obj = app_context(ctx)
    entries = _load_add_entries(file)
    with ctx_obj.client() as client:
        data = client.post("/api/tools-config/entries", json={"entries": entries, "replace": replace})
    emit_result(ctx_obj, data)


@app.command("tools-remove")
@covers(("DELETE", "/api/tools-config/entries/{title}"))
def remove_tools_entry(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="Tools config entry title.")],
) -> None:
    """Remove one tools config entry by title and hot-reload.

    Example: ``tai manifest tools-remove my-entry``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/tools-config/entries/{title}")
    emit_result(ctx_obj, data)


@app.command("agents-add")
@covers(("POST", "/api/agents-config/entries"))
def add_agents_entries(
    ctx: typer.Context,
    file: Annotated[
        Path,
        typer.Option(
            "--file", exists=True, dir_okay=False, readable=True, help="A JSON file with the agents entries to add."
        ),
    ],
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Replace entries whose title already exists (in place) instead of refusing."),
    ] = False,
) -> None:
    """Add agents config entries to the manifest and hot-reload.

    The file is ONE entry object, a bare JSON array of entries, or an object
    carrying an ``"entries"`` list. Without ``--replace`` an entry whose title
    already exists is refused.

    Example: ``tai manifest agents-add --file entries.json``
    """
    ctx_obj = app_context(ctx)
    entries = _load_add_entries(file)
    with ctx_obj.client() as client:
        data = client.post("/api/agents-config/entries", json={"entries": entries, "replace": replace})
    emit_result(ctx_obj, data)


@app.command("agents-remove")
@covers(("DELETE", "/api/agents-config/entries/{title}"))
def remove_agents_entry(
    ctx: typer.Context,
    title: Annotated[str, typer.Argument(help="Agents config entry title.")],
) -> None:
    """Remove one agents config entry by title and hot-reload.

    Example: ``tai manifest agents-remove my-entry``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/agents-config/entries/{title}")
    emit_result(ctx_obj, data)


@app.command("api-tools")
@covers(("POST", "/api/api-tools"))
def update_api_tools(
    ctx: typer.Context,
    include_add: Annotated[
        list[str] | None, typer.Option("--include-add", help="A name to add to the include list (repeatable).")
    ] = None,
    include_remove: Annotated[
        list[str] | None,
        typer.Option("--include-remove", help="A name to remove from the include list (repeatable)."),
    ] = None,
    exclude_add: Annotated[
        list[str] | None, typer.Option("--exclude-add", help="A name to add to the exclude list (repeatable).")
    ] = None,
    exclude_remove: Annotated[
        list[str] | None,
        typer.Option("--exclude-remove", help="A name to remove from the exclude list (repeatable)."),
    ] = None,
) -> None:
    """Add/remove names on the api_tools include/exclude lists and hot-reload.

    At least one of the four options is required.

    Example: ``tai manifest api-tools --include-add echo --exclude-remove echo``
    """
    ctx_obj = app_context(ctx)
    if not (include_add or include_remove or exclude_add or exclude_remove):
        raise typer.BadParameter(
            "nothing to change: give at least one of --include-add/--include-remove/--exclude-add/--exclude-remove"
        )
    body = {
        "include_add": list(include_add) if include_add else [],
        "include_remove": list(include_remove) if include_remove else [],
        "exclude_add": list(exclude_add) if exclude_add else [],
        "exclude_remove": list(exclude_remove) if exclude_remove else [],
    }
    with ctx_obj.client() as client:
        data = client.post("/api/api-tools", json=body)
    emit_result(ctx_obj, data)
