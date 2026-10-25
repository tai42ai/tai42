"""``tai schedules`` — manage scheduled jobs.

Thin wrappers over the ``/api/schedules*`` routes. These depend on an installed
scheduling backend; without one the routes answer a loud 501, surfaced as an error.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer
from pydantic import ValidationError
from tai42_contract.backend import ScheduleCreate

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_records,
    emit_result,
    load_kwargs_arg,
    parse_json_object,
    seg,
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
    """List the configured schedules (name, tool, cadence, enabled, next run).

    Example: ``tai schedules list``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/schedules")
    emit_records(ctx_obj, data, route=("GET", "/api/schedules"))


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
    every: Annotated[
        float | None, typer.Option("--every", help="Run every SECONDS (an interval cadence, > 0).", metavar="SECONDS")
    ] = None,
    cron: Annotated[
        str | None,
        typer.Option(
            "--cron", help="Run on a crontab EXPR (5 fields, or 6 with a leading seconds field).", metavar="EXPR"
        ),
    ] = None,
    cadence: Annotated[
        str | None,
        typer.Option("--cadence", help='The cadence as a JSON object, e.g. {"kind": "interval", "every": 60}.'),
    ] = None,
    schedule_name: Annotated[
        str | None, typer.Option("--schedule-name", help="The schedule's name (needs a cadence).", metavar="NAME")
    ] = None,
    execution_key: Annotated[
        str | None,
        typer.Option(
            "--execution-key",
            help=(
                "The api key a contract-bearing recurring fire runs as (required with any contract jq); "
                "an account is refused."
            ),
        ),
    ] = None,
    start_expr: Annotated[
        str | None,
        typer.Option(
            "--start-expr", help="jq building the fired tool's kwargs over the tool arguments; null starts nothing."
        ),
    ] = None,
    cancel_expr: Annotated[
        str | None, typer.Option("--cancel-expr", help="jq naming the parked interaction ids to cancel.")
    ] = None,
    resume_expr: Annotated[
        str | None, typer.Option("--resume-expr", help="jq naming the parked interactions to resume or take.")
    ] = None,
    extras_expr: Annotated[
        str | None, typer.Option("--extras-expr", help="jq building the extras mapping handed to the started target.")
    ] = None,
    subject: Annotated[
        str | None,
        typer.Option("--subject", help="The schedule-door state context as a JSON object the fire keys its writes on."),
    ] = None,
    state_binding: Annotated[
        str | None, typer.Option("--state-binding", help="The door-layer state binding as a JSON object.")
    ] = None,
) -> None:
    """Create a schedule that runs a tool on a cadence, or once now with no cadence.

    The cadence is one of ``--every`` / ``--cron`` / ``--cadence`` (at most one); with none
    the tool runs once now. The four door-contract jqs (``--start-expr`` / ``--cancel-expr`` /
    ``--resume-expr`` / ``--extras-expr``) make the schedule a parkable-driving door; any of
    them requires ``--execution-key``.

    Example: ``tai schedules add report --cron '0 9 * * *'``
    """
    ctx_obj = app_context(ctx)
    fields: dict[str, Any] = {
        "tool_name": tool_name,
        "tool_kwargs": load_kwargs_arg(
            tool_kwargs,
            tool_kwargs_file,
            tool_kw,
            param_hint="--tool-kwargs",
            file_param_hint="--tool-kwargs-file",
            kw_param_hint="--tool-kw",
        ),
        "cadence": _cadence(every, cron, cadence),
        "schedule_name": schedule_name,
        "execution_key": execution_key,
    }
    for field, value in (
        ("start_expr", start_expr),
        ("cancel_expr", cancel_expr),
        ("resume_expr", resume_expr),
        ("extras_expr", extras_expr),
    ):
        if value is not None:
            fields[field] = {"content": value}
    if subject is not None:
        fields["subject"] = parse_json_object(subject, param_hint="--subject")
    if state_binding is not None:
        fields["state_binding"] = parse_json_object(state_binding, param_hint="--state-binding")
    try:
        body = ScheduleCreate.model_validate(fields)
    except ValidationError as exc:
        first = exc.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "body"
        raise typer.BadParameter(first["msg"], param_hint=field) from exc
    with ctx_obj.client() as client:
        data = client.post("/api/schedules", json=body.model_dump(mode="json", exclude_none=True))
    emit_result(ctx_obj, data)


def _cadence(every: float | None, cron: str | None, cadence: str | None) -> dict[str, Any] | None:
    """The cadence the at-most-one cadence option names, as a ``ScheduleCadence`` mapping; ``None`` for none."""
    given = [
        name for name, value in (("--every", every), ("--cron", cron), ("--cadence", cadence)) if value is not None
    ]
    if len(given) > 1:
        raise typer.BadParameter(f"give at most one cadence option, not {' and '.join(given)}", param_hint=given[1])
    if every is not None:
        return {"kind": "interval", "every": every}
    if cron is not None:
        return {"kind": "crontab", "expression": cron}
    if cadence is not None:
        return parse_json_object(cadence, param_hint="--cadence")
    return None


@app.command("delete")
@covers(("DELETE", "/api/schedules/{schedule_name}"))
def delete_schedule(ctx: typer.Context, name: Annotated[str, typer.Argument(help="Schedule name.")]) -> None:
    """Delete a schedule by name.

    Example: ``tai schedules delete report``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.delete(f"/api/schedules/{seg(name)}")
    emit_result(ctx_obj, data)
