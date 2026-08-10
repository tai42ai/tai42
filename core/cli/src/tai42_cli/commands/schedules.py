"""``tai schedules`` — manage scheduled jobs.

Thin wrappers over the ``/api/schedules*`` routes. These depend on an installed
scheduling backend; without one the routes answer a loud 501, surfaced as an error.
"""

from __future__ import annotations

from typing import Annotated

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_result,
    load_kwargs_arg,
    parse_kwargs,
)

app = typer.Typer(
    name="schedules",
    help="Manage scheduled jobs.",
    no_args_is_help=True,
)

_TOOL_KWARGS_FILE_HELP = (
    "Read the tool arguments JSON object from a file, or from stdin when the path is '-', instead of putting a "
    "secret on the command line (a value on argv leaks via ps and shell history). Mutually exclusive with "
    "--tool-kwargs; --tool-kw pairs still override its keys."
)


@app.command("list")
@covers(("GET", "/api/schedules"))
def list_schedules(ctx: typer.Context) -> None:
    """List the configured schedules.

    Example: ``tai schedules list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/schedules")
    emit_result(ctx_obj, data)


@app.command("server-datetime")
@covers(("GET", "/api/schedules/server-datetime"))
def server_datetime(ctx: typer.Context) -> None:
    """Get the server's current date and time.

    Example: ``tai schedules server-datetime``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/schedules/server-datetime")
    emit_result(ctx_obj, data)


@app.command("add")
@covers(("POST", "/api/schedules"))
def create_schedule(
    ctx: typer.Context,
    tool_name: Annotated[str, typer.Argument(help="The tool to run on a schedule.")],
    tool_kwargs: Annotated[str | None, typer.Option("--tool-kwargs", help="Tool arguments as a JSON object.")] = None,
    tool_kwargs_file: Annotated[str | None, typer.Option("--tool-kwargs-file", help=_TOOL_KWARGS_FILE_HELP)] = None,
    tool_kw: Annotated[
        list[str] | None, typer.Option("--tool-kw", help="A key=value tool argument (repeatable).")
    ] = None,
    schedule_kwargs: Annotated[
        str | None, typer.Option("--schedule-kwargs", help="Schedule cadence params as JSON.")
    ] = None,
    schedule_kw: Annotated[
        list[str] | None, typer.Option("--schedule-kw", help="A key=value schedule param (repeatable).")
    ] = None,
) -> None:
    """Create a schedule that periodically runs a tool.

    Example: ``tai schedules add report --schedule-kw cron='0 9 * * *'``
    """
    ctx_obj = app_context(ctx)
    body = {
        "tool_name": tool_name,
        "tool_kwargs": load_kwargs_arg(
            tool_kwargs,
            tool_kwargs_file,
            tool_kw,
            param_hint="--tool-kwargs",
            file_param_hint="--tool-kwargs-file",
            kw_param_hint="--tool-kw",
        ),
        "schedule_kwargs": parse_kwargs(schedule_kwargs, schedule_kw),
    }
    with ctx_obj.client() as client:
        data = client.post("/api/schedules", json=body)
    emit_result(ctx_obj, data)


@app.command("delete")
@covers(("DELETE", "/api/schedules/{schedule_name}"))
def delete_schedule(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Schedule name.")]) -> None:
    """Delete a schedule by name.

    Example: ``tai schedules delete report``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/schedules/{name}")
    emit_result(ctx_obj, data)
