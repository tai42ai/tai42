"""Runs-index read + retention operations — ``/api/runs``.

The platform-side enumeration of runs, sourced from the skeleton's own
``run_index`` table (NOT the observability vendor): a paginated, filterable list and
the retention prune. Each row carries its ``trace_id`` so a client can deep-link to
the observability trace view for the run.

The list door is AUTHED but a plain ``read`` (the row carries only identity +
outcome metadata, never a run's input/output body — that lives in the vendor trace).
The prune is a deployment-wide destructive purge, so it is ``fenced`` (admin only),
mirroring the checkpoint-retention sweep.

With the skeleton Postgres component unconfigured the surface is cleanly OFF: the
list is the empty page and the prune reports a store-off skip, never reaching for an
absent database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

from pydantic import BaseModel, Field
from tai42_kit.db import component_store_configured

from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.operations import BadRequestError, operation
from tai42_skeleton.runs.models import RunIndexFilter, RunOutcome, RunRow
from tai42_skeleton.runs.settings import run_index_settings
from tai42_skeleton.runs.store import get_run_index_store


class RunsListQuery(BaseModel):
    """The runs-index list door's filters + paging. Spec metadata only — the router
    parses the query at the HTTP edge."""

    preset: str | None = Field(default=None, description="Filter to one preset name.")
    version: int | None = Field(default=None, description="Filter to one preset version.")
    user: str | None = Field(default=None, description="Filter to one run user (the attribution user identity).")
    session: str | None = Field(
        default=None, description="Filter to one run session/thread (the attribution session identity)."
    )
    interaction: str | None = Field(
        default=None,
        description=(
            "Filter to one park lifecycle by interaction id — returns the parked dispatch's row "
            "and its resume dispatch's row."
        ),
    )
    outcome: str | None = Field(
        default=None, description="Filter to one outcome: running | success | error | parked | aborted."
    )
    from_: str | None = Field(
        default=None, alias="from", description="Inclusive lower bound on the run start as an ISO-8601 instant."
    )
    to: str | None = Field(default=None, description="Inclusive upper bound on the run start as an ISO-8601 instant.")
    page: int = Field(default=1, ge=1, description="1-based page number.")
    page_size: int = Field(
        default=50, ge=1, alias="pageSize", description="Items per page; clamped to the deployment cap, never refused."
    )


def _row_view(row: RunRow) -> dict[str, Any]:
    """The list-row wire view — every enumerable field plus the ``trace_id`` a client
    deep-links to the observability trace view with (``None`` when the run has no
    trace), and the ``interactionId`` lifecycle key joining a parked run's row with
    its resume dispatch's row (``None`` for a plain run)."""
    return {
        "runId": row.run_id,
        "preset": row.preset_name,
        "version": row.preset_version,
        "traceId": row.trace_id,
        "user": row.user_id,
        "session": row.session_id,
        "interactionId": row.interaction_id,
        "outcome": row.outcome,
        "startedAt": row.started_at,
        "endedAt": row.ended_at,
    }


@operation(
    summary="List platform runs",
    tags=["runs"],
    errors=[BadRequestError],
    request_model=RunsListQuery,
)
async def list_runs(
    preset: str | None,
    version: int | None,
    user: str | None,
    session: str | None,
    interaction: str | None,
    outcome: str | None,
    t0: datetime | None,
    t1: datetime | None,
    page: int,
    page_size: int,
) -> dict:
    """A newest-first, filterable page of the platform runs index.

    Enumerated from the ``run_index`` table, so runs are listable without the
    observability vendor. With the store OFF the honest answer is the empty page.
    Each item carries ``traceId`` for a deep link to the vendor trace view."""
    if not component_store_configured(SKELETON_COMPONENT):
        return {"items": [], "page": page, "nextPage": None}

    page_size = min(page_size, run_index_settings().max_page_size)
    run_filter = RunIndexFilter(
        preset=preset,
        version=version,
        user=user,
        session=session,
        interaction=interaction,
        # The router validated ``outcome`` against the closed vocabulary at the edge.
        outcome=cast("RunOutcome | None", outcome),
        t0=t0,
        t1=t1,
    )
    rows = await get_run_index_store().list(run_filter, page=page, page_size=page_size)
    next_page = page + 1 if len(rows) >= page_size else None
    return {"items": [_row_view(r) for r in rows], "page": page, "nextPage": next_page}


@operation(
    summary="Prune the platform runs index",
    tags=["runs"],
    destructive=True,
    reload_gated=True,
)
async def prune_runs() -> dict:
    """Delete runs-index rows older than the configured retention window; return the
    window and the number pruned.

    A no-op (nothing deleted) when the store is OFF or ``TAI_RUNS_INDEX_RETENTION_DAYS``
    is unset — each reported in ``skipped``, mirroring the checkpoint-retention sweep's
    disabled-retention posture (unset = rows kept forever)."""
    if not component_store_configured(SKELETON_COMPONENT):
        return {
            "retention_days": None,
            "pruned_count": 0,
            "skipped": "the runs-index store is not configured (skeleton Postgres component unset)",
        }

    retention_days = run_index_settings().retention_days
    if retention_days is None:
        return {
            "retention_days": None,
            "pruned_count": 0,
            "skipped": "retention disabled (TAI_RUNS_INDEX_RETENTION_DAYS unset); runs are kept forever",
        }

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    pruned = await get_run_index_store().prune(cutoff)
    return {"retention_days": retention_days, "pruned_count": pruned, "cutoff": cutoff.isoformat()}
