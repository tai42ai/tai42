"""``tai templates`` — manage prompt and resource templates.

Thin wrappers over the ``/api/templates``, ``/api/template``, ``/api/upload-template``,
``/api/delete-template``, ``/api/delete-template-dir``, ``/api/render-template`` and
``/api/clear-templates-cache`` routes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_result,
    load_json_object_arg,
)

app = typer.Typer(
    name="templates",
    help="Manage prompt and resource templates.",
    no_args_is_help=True,
)

_TEXT_HELP = (
    'The templated text to render as a JSON object: inline Jinja under "content" OR a stored resource id under '
    '"id" (exactly one), plus any render parameters under "kwargs".'
)
_TEXT_FILE_HELP = (
    "Read the templated-text JSON object from a file, or from stdin when the path is '-', instead of putting a "
    "secret on the command line (a value on argv leaks via ps and shell history). Mutually exclusive with --text."
)


@app.command("list")
@covers(("GET", "/api/templates"))
def list_templates(ctx: typer.Context) -> None:
    """List the available templates.

    Example: ``tai templates list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/templates")
    emit_result(ctx_obj, data)


@app.command("get")
@covers(("POST", "/api/template"))
def get_template(ctx: typer.Context, template_id: Annotated[str, typer.Argument(help="Template id.")]) -> None:
    """Fetch a template's content and its input schema.

    Example: ``tai templates get prompts/greeting.md``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post("/api/template", json={"template_id": template_id})
    emit_result(ctx_obj, data)


@app.command("upload")
@covers(("POST", "/api/upload-template"))
def upload_template(
    ctx: typer.Context,
    path: Annotated[str, typer.Argument(help="Template key to write.")],
    file: Annotated[
        Path,
        typer.Option("--file", exists=True, dir_okay=False, readable=True, help="Local file whose content to upload."),
    ],
) -> None:
    """Upload (create or overwrite) a template from a local file.

    Example: ``tai templates upload prompts/greeting.md --file greeting.md``
    """
    ctx_obj = app_context(ctx)
    content = file.read_text(encoding="utf-8")
    with ctx_obj.client() as client:
        data = client.post("/api/upload-template", json={"path": path, "content": content})
    emit_result(ctx_obj, data)


@app.command("delete")
@covers(("POST", "/api/delete-template"))
def delete_template(ctx: typer.Context, path: Annotated[str, typer.Argument(help="Template key to delete.")]) -> None:
    """Delete a template.

    Example: ``tai templates delete prompts/greeting.md``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post("/api/delete-template", json={"path": path})
    emit_result(ctx_obj, data)


@app.command("delete-dir")
@covers(("POST", "/api/delete-template-dir"))
def delete_template_dir(
    ctx: typer.Context, path: Annotated[str, typer.Argument(help="Template directory to delete.")]
) -> None:
    """Delete every template under a directory.

    A directory that matches no templates is a loud 404 (unlike ``delete``, which
    is idempotent for an absent single key).

    Example: ``tai templates delete-dir prompts/archive``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post("/api/delete-template-dir", json={"path": path})
    emit_result(ctx_obj, data)


@app.command("render")
@covers(("POST", "/api/render-template"))
def render_template(
    ctx: typer.Context,
    text: Annotated[str | None, typer.Option("--text", help=_TEXT_HELP)] = None,
    text_file: Annotated[str | None, typer.Option("--text-file", help=_TEXT_FILE_HELP)] = None,
) -> None:
    """Render a templated text by id or inline content with kwargs.

    Example: ``tai templates render --text '{"id": "prompts/greeting.md", "kwargs": {"name": "Ada"}}'``
    """
    ctx_obj = app_context(ctx)
    text_obj = load_json_object_arg(text, text_file, param_hint="--text", file_param_hint="--text-file")
    if text_obj is None:
        raise typer.BadParameter("give one of --text or --text-file", param_hint="--text/--text-file")
    with ctx_obj.client() as client:
        data = client.post("/api/render-template", json={"text": text_obj})
    emit_result(ctx_obj, data)


@app.command("clear-cache")
@covers(("POST", "/api/clear-templates-cache"))
def clear_cache(ctx: typer.Context) -> None:
    """Clear the template render cache.

    Example: ``tai templates clear-cache``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post("/api/clear-templates-cache")
    emit_result(ctx_obj, data)
