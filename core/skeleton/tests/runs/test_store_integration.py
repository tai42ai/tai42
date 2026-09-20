"""A REAL Postgres exercise of the runs-index store: the ``run_index_outcome_check``
CHECK constraint and the ``run_id`` primary key as the durable authority, the two
``COALESCE`` write rules under genuine SQL NULL semantics (``trace_id`` backfills a NULL
and keeps a captured id; ``interaction_id`` is first-set-wins), the inclusive
``timestamptz`` range filters with real tz-aware bounds, the deterministic newest-first
paging with the ``run_id`` tiebreak on equal ``started_at``, and the prune-by-cutoff — SQL
behavior a fake only approximates.

It is OPT-IN: set ``TAI42_SKELETON_REAL_PG=1`` and point ``TAI_DATABASE_DEFAULT_PG_*`` at a
live Postgres. Without the opt-in the tests SKIP VISIBLY with a clear reason (never a
silent skip)."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import LiteralString

import pytest
from psycopg.errors import CheckViolation, UniqueViolation
from tai42_kit.clients import client_ctx
from tai42_kit.clients.base import shutdown_all_clients
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import apply_migrations, component_store_settings
from tai42_kit.settings import reset_all_settings

from tai42_skeleton.db import SKELETON_COMPONENT, skeleton_entry
from tai42_skeleton.runs.models import RunIndexFilter
from tai42_skeleton.runs.store import PostgresRunIndexStore

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_SKELETON_REAL_PG"
_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _iso(offset_seconds: int) -> str:
    return (_BASE + timedelta(seconds=offset_seconds)).isoformat()


async def _exec(sql: LiteralString, params: tuple = ()) -> None:
    async with (
        client_ctx(PostgresClient, component_store_settings(SKELETON_COMPONENT)) as pool,
        pool.connection() as conn,
    ):
        await conn.execute(sql, params)


@pytest.fixture
async def store() -> AsyncIterator[tuple[PostgresRunIndexStore, str]]:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres runs-index store test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs the CHECK constraint + "
            "COALESCE + timestamptz semantics — no fake)"
        )
    reset_all_settings()
    await apply_migrations([skeleton_entry()])
    token = f"it-{uuid.uuid4().hex[:12]}"
    await _exec("DELETE FROM run_index WHERE preset_name = %s", (token,))
    yield PostgresRunIndexStore(), token
    await _exec("DELETE FROM run_index WHERE preset_name = %s", (token,))
    await shutdown_all_clients()


async def _start(store: PostgresRunIndexStore, token: str, run_id: str, **kw) -> None:
    await store.insert_start(
        run_id,
        token,
        kw.get("version", 1),
        trace_id=kw.get("trace_id"),
        user_id=kw.get("user"),
        session_id=kw.get("session"),
        interaction_id=kw.get("interaction"),
        started_at=kw.get("started_at", _iso(0)),
    )


async def test_outcome_check_constraint_rejects_unknown_state(store: tuple[PostgresRunIndexStore, str]) -> None:
    s, token = store
    await _start(s, token, f"{token}-r1")
    # The closed outcome vocabulary is enforced by the real ``run_index_outcome_check``
    # CHECK — an out-of-vocabulary terminal write is rejected by the database, not by any
    # in-process guard.
    with pytest.raises(CheckViolation):
        await s.update_outcome(f"{token}-r1", "finished", _iso(1))  # type: ignore[arg-type]


async def test_run_id_primary_key_rejects_duplicate_start(store: tuple[PostgresRunIndexStore, str]) -> None:
    s, token = store
    await _start(s, token, f"{token}-r1")
    # Each dispatch mints a fresh id; a re-insert is a loud duplicate against the real
    # ``run_id`` primary key.
    with pytest.raises(UniqueViolation):
        await _start(s, token, f"{token}-r1")


async def test_coalesce_trace_backfill_keeps_captured_id(store: tuple[PostgresRunIndexStore, str]) -> None:
    s, token = store
    run = f"{token}-r1"
    await _start(s, token, run, trace_id=None)
    # ``trace_id = COALESCE(%s, trace_id)`` fills a NULL on the terminal write …
    await s.update_outcome(run, "success", _iso(1), trace_id="trace-A")
    [row] = await s.list(RunIndexFilter(preset=token), page=1, page_size=10)
    assert row.trace_id == "trace-A"
    assert row.outcome == "success"
    # … and a later NULL sample never clobbers the captured id.
    await s.update_outcome(run, "success", _iso(2), trace_id=None)
    [row] = await s.list(RunIndexFilter(preset=token), page=1, page_size=10)
    assert row.trace_id == "trace-A"


async def test_coalesce_interaction_is_first_set_wins(store: tuple[PostgresRunIndexStore, str]) -> None:
    s, token = store
    # A resume row captures its ORIGIN id at START; ``interaction_id =
    # COALESCE(interaction_id, %s)`` keeps it even when the body parks again with a new id.
    origin = f"{token}-origin"
    await _start(s, token, f"{token}-resume", interaction=origin)
    await s.update_outcome(f"{token}-resume", "parked", _iso(1), interaction_id=f"{token}-second-park")
    # A plain run captures NULL at START; the park's terminal write fills it.
    await _start(s, token, f"{token}-plain", interaction=None)
    await s.update_outcome(f"{token}-plain", "parked", _iso(1), interaction_id=f"{token}-park-id")

    rows = {r.run_id: r for r in await s.list(RunIndexFilter(preset=token), page=1, page_size=10)}
    assert rows[f"{token}-resume"].interaction_id == origin
    assert rows[f"{token}-plain"].interaction_id == f"{token}-park-id"


async def test_timestamptz_range_filter_is_inclusive(store: tuple[PostgresRunIndexStore, str]) -> None:
    s, token = store
    for i in range(5):
        await _start(s, token, f"{token}-r{i}", started_at=_iso(i * 10))
    # An inclusive ``started_at`` range over the real ``timestamptz`` column: both bounds
    # land on a row, so the window [t=10, t=30] selects exactly r1, r2, r3.
    rows = await s.list(
        RunIndexFilter(preset=token, t0=_BASE + timedelta(seconds=10), t1=_BASE + timedelta(seconds=30)),
        page=1,
        page_size=10,
    )
    assert [r.run_id for r in rows] == [f"{token}-r3", f"{token}-r2", f"{token}-r1"]


async def test_newest_first_paging_breaks_ties_on_run_id(store: tuple[PostgresRunIndexStore, str]) -> None:
    s, token = store
    # Three rows share ONE ``started_at`` — the deterministic tiebreak is ``run_id DESC``, so
    # a keyset-free page still yields a stable total order across equal timestamps.
    await _start(s, token, f"{token}-a", started_at=_iso(0))
    await _start(s, token, f"{token}-b", started_at=_iso(0))
    await _start(s, token, f"{token}-c", started_at=_iso(0))
    seen: list[str] = []
    for page in (1, 2, 3):
        rows = await s.list(RunIndexFilter(preset=token), page=page, page_size=1)
        seen.extend(r.run_id for r in rows)
    assert seen == [f"{token}-c", f"{token}-b", f"{token}-a"]


async def test_prune_deletes_strictly_before_cutoff(store: tuple[PostgresRunIndexStore, str]) -> None:
    s, token = store
    for i in range(4):
        await _start(s, token, f"{token}-r{i}", started_at=_iso(i * 10))
    # ``started_at < cutoff`` is a STRICT real timestamptz comparison: a row exactly on the
    # cutoff survives, everything earlier is deleted, and the rowcount is returned.
    deleted = await s.prune(_BASE + timedelta(seconds=20))
    assert deleted == 2
    remaining = sorted(r.run_id for r in await s.list(RunIndexFilter(preset=token), page=1, page_size=10))
    assert remaining == [f"{token}-r2", f"{token}-r3"]


async def test_started_at_renders_iso_from_real_timestamptz(store: tuple[PostgresRunIndexStore, str]) -> None:
    s, token = store
    run = f"{token}-r1"
    await _start(s, token, run, started_at=_iso(0))
    [row] = await s.list(RunIndexFilter(preset=token), page=1, page_size=10)
    # The DB stores a ``timestamptz``; the store renders it back through ``.isoformat()`` and
    # the round-tripped instant equals the one written (ended_at NULL while running).
    assert row.ended_at is None
    assert datetime.fromisoformat(row.started_at) == _BASE
