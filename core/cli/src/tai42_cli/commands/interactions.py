"""``tai interactions`` — list, stream and answer pending interactions.

``list`` reads one page of the pending questions from ``GET /api/interactions``;
``pending`` reads the operator-only audit of PARKED (async) asks awaiting an answer
from ``GET /api/interactions/pending``; ``stream`` tails the inbox live (a tail-only
add/answered/removed feed, no backlog); ``answer`` posts a human answer; ``cancel``
withdraws a pending ask without answering it.
"""

from __future__ import annotations

from typing import Annotated

import typer

from tai42_cli.commands._common import (
    app_context,
    covers,
    emit_result,
    parse_json_value,
    stream_frames,
)

app = typer.Typer(
    name="interactions",
    help="List, stream and answer pending interactions.",
    no_args_is_help=True,
)


@app.command("list")
@covers(("GET", "/api/interactions"))
def list_interactions(
    ctx: typer.Context,
    page: Annotated[int | None, typer.Option("--page", help="Page number (1-based).")] = None,
    page_size: Annotated[int | None, typer.Option("--page-size", help="Items per page (capped at 200).")] = None,
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="Filter by lifecycle status (pending|answered|cancelled). The inbox holds only pending records.",
        ),
    ] = None,
) -> None:
    """Print one page of pending interactions.

    Example: ``tai interactions list --page 1 --page-size 50 --status pending``
    """
    ctx_obj = app_context(ctx)
    params: dict[str, str] = {}
    if page is not None:
        params["page"] = str(page)
    if page_size is not None:
        params["pageSize"] = str(page_size)
    if status is not None:
        params["status"] = status
    with ctx_obj.client() as client:
        data = client.get("/api/interactions", params=params or None)
    emit_result(ctx_obj, data)


@app.command("pending")
@covers(("GET", "/api/interactions/pending"))
def list_pending_interactions(
    ctx: typer.Context,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Max parked asks to return, 1..1000 (default 500).")
    ] = None,
) -> None:
    """Print the parked (async) interactions awaiting an answer — the operator-only
    audit a watchdog reads to spot asks nearing or past their expiry.

    Example: ``tai interactions pending --limit 100``
    """
    ctx_obj = app_context(ctx)
    params: dict[str, str] = {}
    if limit is not None:
        params["limit"] = str(limit)
    with ctx_obj.client() as client:
        data = client.get("/api/interactions/pending", params=params or None)
    emit_result(ctx_obj, data)


@app.command("stream")
@covers(("GET", "/api/interactions/stream"))
def stream_interactions(ctx: typer.Context) -> None:
    """Tail the interactions inbox live (add/answered/removed events).

    Example: ``tai interactions stream``
    """
    ctx_obj = app_context(ctx)
    stream_frames(ctx_obj, "GET", "/api/interactions/stream")


@app.command("answer")
@covers(("POST", "/api/interactions/{interaction_id}/answer"))
def answer_interaction(
    ctx: typer.Context,
    interaction_id: Annotated[str, typer.Argument(help="Interaction id.")],
    answer: Annotated[str, typer.Option("--answer", help="The answer value as JSON (a string, bool, object, ...).")],
) -> None:
    """Answer a pending interaction. The value is validated server-side.

    Example: ``tai interactions answer i_123 --answer '"yes"'``
    """
    ctx_obj = app_context(ctx)
    value = parse_json_value(answer, param_hint="--answer")
    with ctx_obj.client() as client:
        data = client.post(f"/api/interactions/{interaction_id}/answer", json={"answer": value})
    emit_result(ctx_obj, data)


@app.command("cancel")
@covers(("POST", "/api/interactions/{interaction_id}/cancel"))
def cancel_interaction(
    ctx: typer.Context,
    interaction_id: Annotated[str, typer.Argument(help="Interaction id.")],
) -> None:
    """Cancel a pending interaction — withdraw the ask without answering it.

    The parked flow is never resumed and the interaction's thread is left intact; a
    question that is already answered is a conflict and a gone/expired one is not found.

    Example: ``tai interactions cancel i_123``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.post(f"/api/interactions/{interaction_id}/cancel")
    emit_result(ctx_obj, data)
