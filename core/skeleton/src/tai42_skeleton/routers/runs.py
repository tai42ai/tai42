"""HTTP surface for the platform runs index — ``/api/runs``.

Two thin adapters over the operations in :mod:`tai42_skeleton.operations.runs`:

* ``GET /api/runs`` — a paginated, filterable list of runs (``action="read"``). The
  query string is decoded into the operation's flat params HERE at the HTTP edge (a
  bad filter/page raises ``BadRequestError`` → 400), then the operation runs
  request-free.
* ``POST /api/runs/prune`` — the retention prune (``action="fenced"`` — a
  deployment-wide destructive purge, admin only, exactly like the checkpoint sweep).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from starlette.requests import Request
from tai42_contract.app import tai42_app

from tai42_skeleton.operations import BadRequestError, operation_metadata_of, register_operation_route
from tai42_skeleton.operations.runs import list_runs as _list_runs_op
from tai42_skeleton.operations.runs import prune_runs as _prune_runs_op
from tai42_skeleton.runs.models import RUN_OUTCOMES


def _int(request: Request, key: str, default: int) -> int:
    raw = request.query_params.get(key)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise BadRequestError(f"{key!r} must be an integer") from exc


def _instant(request: Request, key: str) -> datetime | None:
    raw = request.query_params.get(key)
    if raw is None:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise BadRequestError(f"{key!r} must be an ISO-8601 instant") from exc


async def _extract_runs_query(request: Request) -> dict[str, Any]:
    """Decode the runs-list query string into the operation's flat params, raising the
    door's explicit 400 on a malformed filter, page, or time bound."""
    version_raw = request.query_params.get("version")
    version: int | None = None
    if version_raw is not None:
        try:
            version = int(version_raw)
        except ValueError as exc:
            raise BadRequestError("'version' must be an integer") from exc

    outcome = request.query_params.get("outcome")
    if outcome is not None and outcome not in RUN_OUTCOMES:
        raise BadRequestError(f"'outcome' must be one of {sorted(RUN_OUTCOMES)}")

    page = _int(request, "page", 1)
    page_size = _int(request, "pageSize", 50)
    if page < 1:
        raise BadRequestError("'page' must be >= 1")
    if page_size < 1:
        raise BadRequestError("'pageSize' must be >= 1")

    return {
        "preset": request.query_params.get("preset"),
        "version": version,
        "user": request.query_params.get("user"),
        "session": request.query_params.get("session"),
        "interaction": request.query_params.get("interaction"),
        "outcome": outcome,
        "t0": _instant(request, "from"),
        "t1": _instant(request, "to"),
        "page": page,
        "page_size": page_size,
    }


list_runs = register_operation_route(
    tai42_app,
    operation_metadata_of(_list_runs_op),
    path="/api/runs",
    method="GET",
    context_extractor=_extract_runs_query,
    action="read",
)

prune_runs = register_operation_route(
    tai42_app,
    operation_metadata_of(_prune_runs_op),
    path="/api/runs/prune",
    method="POST",
    action="fenced",
)
