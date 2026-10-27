"""Response models for the runs operations: the runs page and the prune result."""

from __future__ import annotations

from pydantic import BaseModel


class RunView(BaseModel):
    """One platform-runs-index row.

    ``traceId`` deep-links the observability trace (``null`` when the run opened
    none); ``interactionId`` joins a parked run with its resume row (``null`` for
    a plain run); ``endedAt`` is ``null`` while the run is still running;
    ``resumedInteractions`` is the parked ids this run resumed or took across its
    span (empty when it resumed nothing).
    """

    runId: str
    preset: str
    version: int | None = None
    traceId: str | None = None
    user: str | None = None
    session: str | None = None
    interactionId: str | None = None
    outcome: str
    startedAt: str
    endedAt: str | None = None
    resumedInteractions: list[str]


class RunsPage(BaseModel):
    """One page of the platform runs index; ``nextPage`` is ``null`` on the last page."""

    items: list[RunView]
    page: int
    nextPage: int | None = None


class RunsPruneResult(BaseModel):
    """The runs-index prune report.

    ``skipped`` is present ONLY on a no-op branch (store off or retention unset);
    ``cutoff`` is present ONLY on a real prune (the ISO cutoff older than which
    rows were deleted).
    """

    retention_days: int | None = None
    pruned_count: int
    skipped: str | None = None
    cutoff: str | None = None
