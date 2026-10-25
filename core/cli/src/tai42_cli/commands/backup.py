"""``tai backup`` — export and import server state.

Thin wrappers over the ``/api/backup/*`` routes. ``export`` streams the raw backup
document to stdout (redirect it to a file); ``import`` consumes exactly that file. A
section whose exporter failed is absent from the document and named under its
``errors`` map; ``export`` prints each one to stderr and exits 1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    echo_stderr,
    emit_records,
    emit_result,
    fetch_download,
)
from tai42_cli.render import strip_control

app = typer.Typer(
    name="backup",
    help="Export and import server state.",
    no_args_is_help=True,
)


@app.command("sections")
@covers(("GET", "/api/backup/sections"))
def list_sections(ctx: typer.Context) -> None:
    """List the registered backup sections.

    Example: ``tai backup sections``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/backup/sections")
    emit_records(ctx_obj, data, route=("GET", "/api/backup/sections"))


@app.command("export")
@covers(("POST", "/api/backup/export"))
def export_backup(
    ctx: typer.Context,
    section: Annotated[list[str], typer.Option("--section", help="A section to export (repeatable).")],
) -> None:
    """Export the named sections as a backup document (printed to stdout).

    Redirect stdout to save it: ``tai backup export --section access_control > backup.json``

    A section the server failed to export is left out of the document: the document is
    still printed, each failed section is named with its message on stderr, and the
    command exits 1.
    """
    ctx_obj = app_context(ctx)
    document = fetch_download(ctx_obj, "POST", "/api/backup/export", json_body={"sections": list(section)})
    typer.echo(document)
    errors: dict[str, str] = json.loads(document)["errors"]
    for name, message in errors.items():
        # Server text reaches the terminal stripped of control characters, as every rendered value does.
        echo_stderr(strip_control(f"backup section {name!r} failed to export: {message}"))
    if errors:
        raise typer.Exit(code=1)


@app.command("import")
@covers(("POST", "/api/backup/import"))
def import_backup(
    ctx: typer.Context,
    file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True, help="A backup document file.")],
    section: Annotated[list[str], typer.Option("--section", help="A section to import (repeatable).")],
) -> None:
    """Import selected sections from a backup document file.

    Example: ``tai backup import backup.json --section access_control``
    """
    ctx_obj = app_context(ctx)
    try:
        document = json.loads(file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"backup file must be valid JSON: {exc}", param_hint="FILE") from exc
    with ctx_obj.client() as client:
        data = client.post("/api/backup/import", json={"document": document, "sections": list(section)})
    emit_result(ctx_obj, data)
