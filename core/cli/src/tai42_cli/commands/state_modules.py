"""``tai state-modules`` — manage platform state-module documents (``/api/state-modules*``).

A state module is a reusable schema fragment plus its parameters, write regimes,
mount-time declarations and trace switch — the platform half of a module document, mounted
onto a state through ``tai states mount``. The document is read from a JSON ``--data``
string, a ``--file`` path, or stdin.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_result,
    parse_json_value,
)

app = typer.Typer(name="state-modules", help="Manage platform state-module documents.", no_args_is_help=True)


def _read_document(data: str | None, file: Path | None) -> Any:
    sources = [s for s in (data is not None, file is not None) if s]
    if len(sources) > 1:
        raise typer.BadParameter("pass the document through only one of --data / --file")
    if data is not None:
        return parse_json_value(data, param_hint="--data")
    if file is not None:
        return parse_json_value(file.read_text(), param_hint="--file")
    text = sys.stdin.read()
    if not text.strip():
        raise typer.BadParameter("no document on stdin (pass --data, --file, or pipe JSON)")
    return parse_json_value(text, param_hint="stdin")


@app.command("list")
@covers(("GET", "/api/state-modules"))
def list_state_modules(ctx: typer.Context) -> None:
    """List every platform state-module document."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get("/api/state-modules"))


@app.command("get")
@covers(("GET", "/api/state-modules/{name}"))
def get_state_module(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The module name.")]) -> None:
    """Show one state-module document."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get(f"/api/state-modules/{name}"))


@app.command("put")
@covers(("PUT", "/api/state-modules/{name}"))
def put_state_module(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The module name.")],
    data: Annotated[str | None, typer.Option("--data", help="The module document JSON.")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="A file holding the module document JSON.")] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Overwrite an existing module of this name.")] = False,
) -> None:
    """Create or replace a state-module document."""
    ctx_obj = app_context(ctx)
    body = _read_document(data, file)
    params = {"replace": "true"} if replace else None
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.put(f"/api/state-modules/{name}", json=body, params=params))


@app.command("delete")
@covers(("DELETE", "/api/state-modules/{name}"))
def delete_state_module(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The module name.")]) -> None:
    """Delete a state-module document (refused while it is mounted)."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.delete(f"/api/state-modules/{name}"))
