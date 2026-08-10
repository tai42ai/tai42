"""``tai storage`` — inspect and manage the storage provider's resources.

Thin wrappers over the authed ``/api/storage*`` routes. Storage is dead by
default; the identity command reports whether a provider is installed, and the
CRUD commands answer the route's honest 501 when none is.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Annotated

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_records,
    emit_result,
    fetch_download,
)

app = typer.Typer(
    name="storage",
    help="Inspect and manage the storage provider's resources.",
    no_args_is_help=True,
)


@app.command("info")
@covers(("GET", "/api/storage"))
def storage_info(ctx: typer.Context) -> None:
    """Show the registered storage provider's identity (or the empty state).

    Example: ``tai storage info``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/storage")
    emit_result(ctx_obj, data)


@app.command("list")
@covers(("GET", "/api/storage/resources"))
def list_resources(ctx: typer.Context) -> None:
    """List the storage resource ids.

    Example: ``tai storage list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/storage/resources")
    emit_records(ctx_obj, data, ["id"], items_key="resources")


@app.command("stat")
@covers(("GET", "/api/storage/resources/{resource_id:path}/stat"))
def stat_resource(ctx: typer.Context, resource_id: Annotated[str, typer.Argument(help="Resource id.")]) -> None:
    """Show a resource's inferred content type.

    Example: ``tai storage stat images/logo.png``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get(f"/api/storage/resources/{resource_id}/stat")
    emit_result(ctx_obj, data)


@app.command("download")
@covers(("GET", "/api/storage/resources/{resource_id:path}/content"))
def download_resource(ctx: typer.Context, resource_id: Annotated[str, typer.Argument(help="Resource id.")]) -> None:
    """Download a resource's raw content to stdout.

    Example: ``tai storage download notes/todo.txt``
    """
    ctx_obj = app_context(ctx)
    typer.echo(fetch_download(ctx_obj, "GET", f"/api/storage/resources/{resource_id}/content"))


@app.command("upload")
@covers(("POST", "/api/storage/resources"))
def upload_resource(
    ctx: typer.Context,
    resource_id: Annotated[str, typer.Argument(help="Resource id.")],
    text: Annotated[str | None, typer.Option("--text", help="Store this text verbatim.")] = None,
    content_base64: Annotated[str | None, typer.Option("--base64", help="Store these base64-encoded bytes.")] = None,
    file: Annotated[
        str | None,
        typer.Option(
            "--file",
            help="Store the raw bytes read from this file path, or from stdin when the path is '-', keeping secret "
            "content off the command line (a value on argv leaks via ps and shell history).",
        ),
    ] = None,
) -> None:
    """Upload a resource — exactly one of ``--text``, ``--base64``, or ``--file`` (a
    path, or ``-`` to read the raw bytes from stdin). An existing id is overwritten.

    Example: ``tai storage upload notes/todo.txt --text 'buy milk'``
    """
    ctx_obj = app_context(ctx)
    if sum(source is not None for source in (text, content_base64, file)) != 1:
        raise typer.BadParameter("pass exactly one of --text, --base64, or --file", param_hint="--text/--base64/--file")
    body: dict = {"id": resource_id}
    if text is not None:
        body["content_text"] = text
    elif content_base64 is not None:
        body["content_base64"] = content_base64
    elif file is not None:
        if file == "-":
            raw = sys.stdin.buffer.read()
        else:
            try:
                raw = Path(file).read_bytes()
            except OSError as exc:
                raise typer.BadParameter(f"cannot read {file!r}: {exc}", param_hint="--file") from exc
        body["content_base64"] = base64.b64encode(raw).decode("ascii")
    with ctx_obj.client() as client:
        data = client.post("/api/storage/resources", json=body)
    emit_result(ctx_obj, data)


@app.command("delete")
@covers(("DELETE", "/api/storage/resources/{resource_id:path}"))
def delete_resource(ctx: typer.Context, resource_id: Annotated[str, typer.Argument(help="Resource id.")]) -> None:
    """Delete a storage resource.

    Example: ``tai storage delete notes/todo.txt``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/storage/resources/{resource_id}")
    emit_result(ctx_obj, data)


@app.command("delete-dir")
@covers(("DELETE", "/api/storage/dirs/{dir_path:path}"))
def delete_dir(ctx: typer.Context, dir_path: Annotated[str, typer.Argument(help="Directory path.")]) -> None:
    """Delete a storage directory subtree.

    Example: ``tai storage delete-dir notes``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/storage/dirs/{dir_path}")
    emit_result(ctx_obj, data)
