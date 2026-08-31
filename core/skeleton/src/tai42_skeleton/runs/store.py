"""Postgres-backed store for the runs index (the ``run_index`` table).

Mirrors the versioned-document store's transport idiom: Postgres is reached through
the app-pooled ``PostgresClient`` bound to the skeleton component, so it shares one
pool per DSN with the other durable stores and is closed centrally at shutdown. It is
a flat, append-then-terminally-update log — no versioning, no cache — since a run row
is written once at dispatch start and updated once at completion.

The list query uses the STATIC optional-filter idiom ``(%s IS NULL OR col = %s)`` for
every filter so the SQL text is invariant regardless of which filters are set (the
planner still uses the per-column indexes for the set clauses). Keeping the statement
static keeps a caller from having to assemble SQL per request and keeps the read one
prepared shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.db import SKELETON_COMPONENT
from tai42_skeleton.runs.models import RunIndexFilter, RunOutcome, RunRow

# The static optional-filter list query. Each ``(%s::type IS NULL OR col = %s)`` clause
# is inert when its param is NULL (unset filter) and an equality/range match otherwise;
# the explicit cast types the NULL param for Postgres. Ordered newest-first with a
# ``run_id`` tiebreak so paging is deterministic across equal ``started_at``.
_LIST_SQL = (
    "SELECT run_id, preset_name, preset_version, trace_id, user_id, session_id, "
    "interaction_id, outcome, started_at, ended_at FROM run_index "
    "WHERE (%s::text IS NULL OR preset_name = %s) "
    "AND (%s::int IS NULL OR preset_version = %s) "
    "AND (%s::text IS NULL OR user_id = %s) "
    "AND (%s::text IS NULL OR session_id = %s) "
    "AND (%s::text IS NULL OR interaction_id = %s) "
    "AND (%s::text IS NULL OR outcome = %s) "
    "AND (%s::timestamptz IS NULL OR started_at >= %s) "
    "AND (%s::timestamptz IS NULL OR started_at <= %s) "
    "ORDER BY started_at DESC, run_id DESC LIMIT %s OFFSET %s"
)


class PostgresRunIndexStore:
    """Postgres implementation of the runs index."""

    @asynccontextmanager
    async def _cursor(self) -> AsyncIterator[Any]:
        async with (
            client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
            pool.connection() as conn,
            conn.cursor() as cur,
        ):
            yield cur

    async def insert_start(
        self,
        run_id: str,
        preset_name: str,
        preset_version: int,
        *,
        trace_id: str | None,
        user_id: str | None,
        session_id: str | None,
        interaction_id: str | None,
        started_at: str,
    ) -> None:
        """Persist a run's START row: ``outcome='running'``, ``ended_at`` NULL.
        ``interaction_id`` is the ambient resume origin when this dispatch is a
        continuation fire (``None`` otherwise). The ``run_id`` primary key makes a
        re-insert of the same run a loud duplicate — a caller bug, since each dispatch
        mints a fresh id."""
        async with self._cursor() as cur:
            await cur.execute(
                "INSERT INTO run_index "
                "(run_id, preset_name, preset_version, trace_id, user_id, session_id, interaction_id, "
                "outcome, started_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'running', %s)",
                (run_id, preset_name, preset_version, trace_id, user_id, session_id, interaction_id, started_at),
            )

    async def update_outcome(
        self,
        run_id: str,
        outcome: RunOutcome,
        ended_at: str,
        *,
        trace_id: str | None = None,
        interaction_id: str | None = None,
    ) -> None:
        """Terminally update a run: set its ``outcome`` + ``ended_at`` and BACKFILL
        ``trace_id`` when one is now available. ``COALESCE(%s, trace_id)`` keeps a
        trace id captured at START and only fills a NULL — a later NULL sample never
        clobbers a captured id. ``interaction_id`` (a park's sentinel id) fills the
        OPPOSITE way — ``COALESCE(interaction_id, %s)``, first-set wins — so a resume
        row keeps the ORIGIN id captured at START even when its own body parks again
        (a single-park lifecycle joins fully; a re-park's new id is deliberately not
        recorded, so deeper chains are not walkable by this column)."""
        async with self._cursor() as cur:
            await cur.execute(
                "UPDATE run_index SET outcome = %s, ended_at = %s, trace_id = COALESCE(%s, trace_id), "
                "interaction_id = COALESCE(interaction_id, %s) WHERE run_id = %s",
                (outcome, ended_at, trace_id, interaction_id, run_id),
            )

    async def list(self, filter: RunIndexFilter, *, page: int, page_size: int) -> list[RunRow]:
        """A newest-first page of run rows matching ``filter``. ``page`` is 1-based;
        the slice is ``LIMIT page_size OFFSET (page-1)*page_size``."""
        offset = (page - 1) * page_size
        version = filter.version
        outcome = filter.outcome
        params = (
            filter.preset,
            filter.preset,
            version,
            version,
            filter.user,
            filter.user,
            filter.session,
            filter.session,
            filter.interaction,
            filter.interaction,
            outcome,
            outcome,
            filter.t0,
            filter.t0,
            filter.t1,
            filter.t1,
            page_size,
            offset,
        )
        async with self._cursor() as cur:
            await cur.execute(_LIST_SQL, params)
            rows = await cur.fetchall()
        return [_row(r) for r in rows]

    async def prune(self, cutoff: datetime) -> int:
        """Delete every run whose ``started_at`` is strictly before ``cutoff``; return
        the number deleted."""
        async with self._cursor() as cur:
            await cur.execute("DELETE FROM run_index WHERE started_at < %s", (cutoff,))
            return cur.rowcount


def _row(row: Any) -> RunRow:
    (
        run_id,
        preset_name,
        preset_version,
        trace_id,
        user_id,
        session_id,
        interaction_id,
        outcome,
        started_at,
        ended_at,
    ) = row
    return RunRow(
        run_id=run_id,
        preset_name=preset_name,
        preset_version=preset_version,
        trace_id=trace_id,
        user_id=user_id,
        session_id=session_id,
        interaction_id=interaction_id,
        outcome=outcome,
        started_at=_iso(started_at),
        ended_at=_iso(ended_at) if ended_at is not None else None,
    )


def _iso(value: Any) -> str:
    """Render a DB ``timestamptz`` as an ISO string, tolerating a store/fake that
    already yields a string."""
    return value if isinstance(value, str) else value.isoformat()


def get_run_index_store() -> PostgresRunIndexStore:
    """The process runs-index store. A plain constructor today (the store holds no
    state — the pool is app-owned); a function so consumers and tests reach it through
    one seam."""
    return PostgresRunIndexStore()
