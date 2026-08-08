"""``tai fleet`` — inspect the backend identity and drive fleet ops.

Thin wrappers over the authed ``/api/backend`` (identity) and ``/api/fleet/*``
(census + soft-restart) routes. The group is named ``fleet`` (not ``backend``)
because the ``tai backend`` command is the re-homed runtime launcher; a
``backend`` group would clobber it. ``info`` reports whether a task backend is
installed; ``workers`` lists the whole bus fleet and ``reload-config`` soft-restarts
it — both work with or without a task backend.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated, Any

import typer

from tai42_skeleton.cli.commands._common import (
    app_context,
    covers,
    emit_result,
)
from tai42_skeleton.cli.render import print_json, print_records

app = typer.Typer(
    name="fleet",
    help="Inspect the execution backend and drive fleet ops.",
    no_args_is_help=True,
)


@app.command("info")
@covers(("GET", "/api/backend"))
def fleet_info(ctx: typer.Context) -> None:
    """Show the registered backend's identity (or the empty state).

    Example: ``tai fleet info``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/backend")
    emit_result(ctx_obj, data)


_WORKER_COLUMNS = ["name", "kind", "pid", "gen", "state", "seen-since", "last-op"]


def _relative_since(beat_at: str | None) -> str:
    """A coarse ``<n><unit> ago`` rendering of a presence ``beat_at`` for the human
    table's seen-since column. ``—`` when the stamp is missing or unparseable."""
    if not beat_at:
        return "—"
    try:
        then = datetime.fromisoformat(beat_at)
        seconds = max(0.0, (datetime.now(UTC) - then).total_seconds())
    except (ValueError, TypeError):
        return "—"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return f"{int(seconds)}s ago"


def _worker_display_row(worker: Mapping[str, Any]) -> dict[str, Any]:
    """Project one raw API worker row into the human table's display columns. The
    ``state`` cell is suffixed ``(stale)`` when the server's ``stale`` flag is set (no
    client-side threshold); ``last-op`` is ``op:outcome`` or ``—`` when the worker has
    applied none."""
    state = str(worker.get("state", ""))
    if worker.get("stale"):
        state = f"{state} (stale)"
    last_op = worker.get("last_op")
    return {
        "name": worker.get("name", ""),
        "kind": worker.get("kind", ""),
        "pid": worker.get("pid", ""),
        "gen": worker.get("generation", ""),
        "state": state,
        "seen-since": _relative_since(worker.get("beat_at")),
        "last-op": f"{last_op['op']}:{last_op['outcome']}" if isinstance(last_op, Mapping) else "—",
    }


@app.command("workers")
@covers(("GET", "/api/fleet/workers"))
def list_workers(ctx: typer.Context) -> None:
    """List the live worker fleet — every process on the bus.

    Example: ``tai fleet workers``
    """
    ctx_obj = app_context(ctx)
    with ctx_obj.client() as client:
        data = client.get("/api/fleet/workers")
    # ``--json`` emits the RAW API rows (structured values); the projected display dict
    # (gen, seen-since, last-op, the stale suffix) is built on the human table path only.
    if ctx_obj.json_output:
        print_json(data)
        return
    workers = data.get("workers", []) if isinstance(data, Mapping) else []
    rows = [_worker_display_row(worker) for worker in workers]
    print_records(rows, _WORKER_COLUMNS, json_output=False)


@app.command("reload-config")
@covers(("POST", "/api/fleet/reload-config"))
def reload_config(
    ctx: typer.Context,
    target: Annotated[
        list[str] | None,
        typer.Option("--target", help="A worker slot name to restrict the reload to (e.g. serve-1; repeatable)."),
    ] = None,
) -> None:
    """Soft-restart the worker fleet — all of them, or only ``--target`` ones.

    The response is the per-worker fleet report (one row per worker with its
    reload outcome). Example: ``tai fleet reload-config --target serve-1``
    """
    ctx_obj = app_context(ctx)
    targets = list(target) if target else None
    with ctx_obj.client() as client:
        data = client.post("/api/fleet/reload-config", json={"targets": targets})
    emit_result(ctx_obj, data)
