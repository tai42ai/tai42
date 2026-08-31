"""Data shapes for the runs index: the terminal outcome vocabulary, the persisted
row, and the read filter.

All generic and store-facing — the store never interprets ``user_id``/``session_id``
beyond equality, and the outcome set is the closed vocabulary the ``run_index``
CHECK constraint enforces.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, get_args

# The closed outcome vocabulary the ``run_index_outcome_check`` constraint enforces:
# ``running`` is the dispatch-start state (``ended_at`` still NULL); ``success`` /
# ``error`` are the normal terminal states; ``parked`` is a run whose tool async-parked
# (returned a ``SuspendedInteraction``) — a terminal record for THIS dispatch even
# though the deferred answer is delivered out of band, so recording ``success`` over the
# unfinished work would be a lie (mirrors the tool-run surface's ``parked``);
# ``aborted`` is a run whose dispatch was CANCELLED (``asyncio.CancelledError`` — a
# user abort, a shutdown drain, an epoch retire) — cancelled is not failed, so it is
# never conflated with ``error``.
RunOutcome = Literal["running", "success", "error", "parked", "aborted"]

# The terminal outcomes a filter may select — ``running`` included, so a caller can
# enumerate in-flight / crash-interrupted runs (a ``running`` row with a NULL
# ``ended_at``).
RUN_OUTCOMES: frozenset[str] = frozenset(get_args(RunOutcome))


@dataclass(frozen=True)
class RunRow:
    """One persisted runs-index row, as the store reads it back.

    ``trace_id`` is the monitoring trace for deep-linking — NULLABLE and best-effort
    (a run whose body opens no trace, or a deployment with monitoring disabled, has
    none). ``interaction_id`` is the lifecycle-correlation key — NULLABLE: a parked
    row carries the interaction id its ``SuspendedInteraction`` sentinel parked on,
    and the later out-of-band resume dispatch's row carries the SAME id (the ambient
    resume origin the continuation drive deposits), so one equality query joins a
    logical run's parked and resume rows; a plain run has none. ``ended_at`` is
    ``None`` while the run is still ``running`` (in-flight or crash-interrupted).
    Timestamps are ISO-8601 strings, matching the versioned-store row convention
    (the DB ``timestamptz`` rendered via ``.isoformat()``)."""

    run_id: str
    preset_name: str
    preset_version: int
    trace_id: str | None
    user_id: str | None
    session_id: str | None
    interaction_id: str | None
    outcome: RunOutcome
    started_at: str
    ended_at: str | None


@dataclass(frozen=True)
class RunIndexFilter:
    """The read filter the list door passes the store — every field optional, an unset
    field is not filtered on. ``preset`` / ``version`` / ``user`` / ``session`` /
    ``interaction`` are equality matches (``interaction`` selects every row of one
    park's lifecycle — the parked dispatch and its resume); ``outcome`` is one of
    :data:`RUN_OUTCOMES`; ``t0`` / ``t1`` are an inclusive ``started_at`` range."""

    preset: str | None = None
    version: int | None = None
    user: str | None = None
    session: str | None = None
    interaction: str | None = None
    outcome: RunOutcome | None = None
    t0: datetime | None = None
    t1: datetime | None = None
