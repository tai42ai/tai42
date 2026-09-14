"""``tai runs`` — list and prune the platform runs index.

Thin wrappers over the ``/api/runs`` routes: the paginated, filterable list read and
the retention prune (a fenced, deployment-wide purge, exactly like the checkpoint
sweep).
"""

from __future__ import annotations

from typing import Annotated

import typer

from tai42_cli.commands._common import app_context, compact, covers, emit_records, emit_result

app = typer.Typer(
    name="runs",
    help="List and prune the platform runs index.",
    no_args_is_help=True,
)


@app.command("list")
@covers(("GET", "/api/runs"))
def list_runs(
    ctx: typer.Context,
    preset: Annotated[str | None, typer.Option("--preset", help="Filter to one preset name.")] = None,
    version: Annotated[int | None, typer.Option("--version", help="Filter to one preset version.")] = None,
    user: Annotated[
        str | None, typer.Option("--user", help="Filter to one run user (attribution user identity).")
    ] = None,
    session: Annotated[
        str | None, typer.Option("--session", help="Filter to one run session/thread (attribution session identity).")
    ] = None,
    interaction: Annotated[
        str | None,
        typer.Option(
            "--interaction",
            help="Filter to one park lifecycle by interaction id (the parked run and its resume).",
        ),
    ] = None,
    outcome: Annotated[
        str | None,
        typer.Option("--outcome", help="Filter to one outcome: running, success, error, parked, or aborted."),
    ] = None,
    from_: Annotated[
        str | None, typer.Option("--from", help="Inclusive lower bound on the run start as an ISO-8601 instant.")
    ] = None,
    to: Annotated[
        str | None, typer.Option("--to", help="Inclusive upper bound on the run start as an ISO-8601 instant.")
    ] = None,
    page: Annotated[int | None, typer.Option("--page", help="Page number (1-based).")] = None,
    page_size: Annotated[int | None, typer.Option("--page-size", help="Rows per page.")] = None,
) -> None:
    """List platform runs, newest first, filtered by the given flags.

    Example: ``tai runs list --preset support --outcome error``
    """
    ctx_obj = app_context(ctx)
    params: dict[str, str] = compact(
        {
            "preset": preset,
            "version": str(version) if version is not None else None,
            "user": user,
            "session": session,
            "interaction": interaction,
            "outcome": outcome,
            "from": from_,
            "to": to,
            "page": str(page) if page is not None else None,
            "pageSize": str(page_size) if page_size is not None else None,
        }
    )
    with ctx_obj.client() as client:
        data = client.get("/api/runs", params=params or None)
    emit_records(ctx_obj, data, route=("GET", "/api/runs"))


@app.command("prune")
@covers(("POST", "/api/runs/prune"))
def prune(ctx: typer.Context) -> None:
    """Delete runs-index rows older than the configured retention window.

    A deployment-wide destructive purge (admin only), mirroring the checkpoint sweep.

    Example: ``tai runs prune``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post("/api/runs/prune")
    emit_result(ctx_obj, data)
