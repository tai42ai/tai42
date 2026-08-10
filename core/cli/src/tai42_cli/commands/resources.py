"""``tai resources`` — load stored resources by id.

Thin wrapper over the ``/api/resources/get`` route.
"""

from __future__ import annotations

from typing import Annotated

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_result,
    load_kwargs_arg,
)

app = typer.Typer(
    name="resources",
    help="Load stored resources by id.",
    no_args_is_help=True,
)

_KWARGS_FILE_HELP = (
    "Read the render kwargs JSON object from a file, or from stdin when the path is '-' (implies --render), instead "
    "of putting a secret on the command line (a value on argv leaks via ps and shell history). Mutually exclusive "
    "with --kwargs; --kw pairs still override its keys."
)


@app.command("get")
@covers(("GET", "/api/resources/get"), ("POST", "/api/resources/get"))
def get_resource_by_id(
    ctx: typer.Context,
    resource_id: Annotated[str, typer.Argument(help="Resource id (path) to load.")],
    render: Annotated[
        bool, typer.Option("--render", help="Render the resource as a Jinja template (with any --kw/--kwargs vars).")
    ] = False,
    kwargs: Annotated[
        str | None, typer.Option("--kwargs", help="Render kwargs as a JSON object (implies --render).")
    ] = None,
    kwargs_file: Annotated[str | None, typer.Option("--kwargs-file", help=_KWARGS_FILE_HELP)] = None,
    kw: Annotated[
        list[str] | None, typer.Option("--kw", help="A key=value render kwarg (repeatable; implies --render).")
    ] = None,
) -> None:
    """Load a stored resource by id, optionally rendering it as a template.

    Without ``--render``/``--kw``/``--kwargs``/``--kwargs-file`` the loaded content is
    returned as-is. Any render var (or a bare ``--render``) renders text as a Jinja template.

    Example: ``tai resources get prompts/greeting.md --kw name=Ada``
    """
    ctx_obj = app_context(ctx)
    wants_render = render or kwargs is not None or kwargs_file is not None or kw
    with ctx_obj.client() as client:
        if wants_render:
            # The render path carries arbitrary nested ``template_kwargs``, which needs
            # a request body — the write-classed POST door.
            render_kwargs = load_kwargs_arg(
                kwargs, kwargs_file, kw, param_hint="--kwargs", file_param_hint="--kwargs-file", kw_param_hint="--kw"
            )
            body = {"resource_id": resource_id, "template_kwargs": render_kwargs}
            data = client.post("/api/resources/get", json=body)
        else:
            # The plain fetch-as-is path — the read-classed GET door, so a resources
            # READ grant can fetch it.
            data = client.get("/api/resources/get", params={"resource_id": resource_id})
    emit_result(ctx_obj, data)
