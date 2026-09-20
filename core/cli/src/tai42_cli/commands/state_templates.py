"""``tai state-templates`` — manage platform state-template documents (``/api/state-templates*``).

A state template is a reusable schema fragment plus its parameters, write regimes,
attach-time declarations and trace switch — the platform half of a template document,
attached onto a state through ``tai states attach``. The document is read from a JSON
``--data`` string, a ``--file`` path, or stdin.
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
    seg,
)

app = typer.Typer(name="state-templates", help="Manage platform state-template documents.", no_args_is_help=True)


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
@covers(("GET", "/api/state-templates"))
def list_state_templates(ctx: typer.Context) -> None:
    """List every platform state-template document."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get("/api/state-templates"))


@app.command("get")
@covers(("GET", "/api/state-templates/{name}"))
def get_state_template(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The template name.")]) -> None:
    """Show one state-template document."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.get(f"/api/state-templates/{seg(name)}"))


@app.command("put")
@covers(("PUT", "/api/state-templates/{name}"))
def put_state_template(
    ctx: typer.Context,
    name: Annotated[str, typer.Argument(help="The template name.")],
    data: Annotated[str | None, typer.Option("--data", help="The template document JSON.")] = None,
    file: Annotated[Path | None, typer.Option("--file", help="A file holding the template document JSON.")] = None,
    replace: Annotated[bool, typer.Option("--replace", help="Overwrite an existing template of this name.")] = False,
) -> None:
    """Create or replace a state-template document."""
    ctx_obj = app_context(ctx)
    body = _read_document(data, file)
    params = {"replace": "true"} if replace else None
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.put(f"/api/state-templates/{seg(name)}", json=body, params=params))


@app.command("delete")
@covers(("DELETE", "/api/state-templates/{name}"))
def delete_state_template(ctx: typer.Context, name: Annotated[str, typer.Argument(help="The template name.")]) -> None:
    """Delete a state-template document (refused while it is attached)."""
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        emit_result(ctx_obj, client.delete(f"/api/state-templates/{seg(name)}"))
