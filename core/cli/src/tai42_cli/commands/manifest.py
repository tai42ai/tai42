"""``tai manifest`` — inspect the server manifest.

``show`` reads the live manifest over ``/api/manifest``, ``plugins`` lists the
installed studio plugins over ``/api/plugins``, and ``replace`` installs a manifest
file over ``/api/manifest/replace``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

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
