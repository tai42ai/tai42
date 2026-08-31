"""``PostgresRunIndexStore`` semantics over the fake Postgres.

Exercises the flat runs-index store: the start/terminal write pair, the trace-id
backfill (COALESCE keeps a captured id, fills a NULL), the lifecycle-correlation
``interaction_id`` (first-set wins: a resume origin captured at START survives a
later park id; a NULL is filled by the park's terminal write), the
duplicate-``run_id`` guard, the filtered + paged list (newest-first, every filter
dimension, and the time range), and the prune-by-cutoff.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from psycopg.errors import UniqueViolation

from tai42_skeleton.runs.models import RunIndexFilter
from tai42_skeleton.runs.store import PostgresRunIndexStore

_BASE = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def store() -> PostgresRunIndexStore:
    return PostgresRunIndexStore()


def _iso(offset_seconds: int) -> str:
    return (_BASE + timedelta(seconds=offset_seconds)).isoformat()


async def _seed(store, pg, run_id, *, preset="wx", version=1, user=None, session=None, interaction=None, at=0):
    await store.insert_start(
        run_id,
        preset,
        version,
        trace_id=None,
        user_id=user,
        session_id=session,
        interaction_id=interaction,
        started_at=_iso(at),
    )


async def test_start_then_terminal_round_trip(pg, store):
    await store.insert_start(
        "r1",
        "weather",
        3,
        trace_id=None,
        user_id="person-1",
        session_id="thread-9",
        interaction_id=None,
        started_at=_iso(0),
    )
    # The START row is enumerable immediately: running, no ended_at.
    [row] = await store.list(RunIndexFilter(), page=1, page_size=10)
    assert (row.run_id, row.preset_name, row.preset_version) == ("r1", "weather", 3)
    assert (row.user_id, row.session_id) == ("person-1", "thread-9")
    assert row.outcome == "running"
    assert row.ended_at is None
    assert row.trace_id is None
    assert row.interaction_id is None

    await store.update_outcome("r1", "success", _iso(2), trace_id="trace-abc")
    [row] = await store.list(RunIndexFilter(), page=1, page_size=10)
    assert row.outcome == "success"
    assert row.ended_at == _iso(2)
    assert row.trace_id == "trace-abc"


async def test_trace_id_captured_at_start_is_not_clobbered_by_null_backfill(pg, store):
    await store.insert_start(
        "r1", "wx", 1, trace_id="trace-start", user_id=None, session_id=None, interaction_id=None, started_at=_iso(0)
    )
    # A terminal write with no trace id must NOT null out the id captured at start.
    await store.update_outcome("r1", "success", _iso(1), trace_id=None)
    [row] = await store.list(RunIndexFilter(), page=1, page_size=10)
    assert row.trace_id == "trace-start"


async def test_park_terminal_write_fills_a_null_interaction_id(pg, store):
    # A fresh dispatch that parks: NULL at START, the sentinel's id lands terminally.
    await _seed(store, pg, "r1")
    await store.update_outcome("r1", "parked", _iso(1), interaction_id="i-1")
    [row] = await store.list(RunIndexFilter(), page=1, page_size=10)
    assert row.interaction_id == "i-1"


async def test_resume_origin_captured_at_start_wins_over_a_later_park(pg, store):
    # First-set wins: a resume dispatch that parks AGAIN keeps its ORIGIN id — the
    # column links each park with its direct resume, never rewritten mid-lifecycle.
    await _seed(store, pg, "r2", interaction="i-origin")
    await store.update_outcome("r2", "parked", _iso(1), interaction_id="i-second-park")
    [row] = await store.list(RunIndexFilter(), page=1, page_size=10)
    assert row.interaction_id == "i-origin"


async def test_lifecycle_rows_are_joinable_by_one_interaction_query(pg, store):
    # The standard park lifecycle: the parked dispatch's row and the later resume
    # dispatch's row share the interaction id — one equality filter returns both.
    await _seed(store, pg, "parked-run", at=0)
    await store.update_outcome("parked-run", "parked", _iso(1), interaction_id="i-life")
    await _seed(store, pg, "resume-run", interaction="i-life", at=2)
    await store.update_outcome("resume-run", "success", _iso(3))
    await _seed(store, pg, "unrelated", at=4)

    rows = await store.list(RunIndexFilter(interaction="i-life"), page=1, page_size=10)
    assert {r.run_id for r in rows} == {"parked-run", "resume-run"}


async def test_duplicate_run_id_raises(pg, store):
    await _seed(store, pg, "dup")
    with pytest.raises(UniqueViolation):
        await _seed(store, pg, "dup")


async def test_list_is_newest_first_and_paginates(pg, store):
    for i in range(5):
        await _seed(store, pg, f"r{i}", at=i)
    page1 = await store.list(RunIndexFilter(), page=1, page_size=2)
    page2 = await store.list(RunIndexFilter(), page=2, page_size=2)
    page3 = await store.list(RunIndexFilter(), page=3, page_size=2)
    assert [r.run_id for r in page1] == ["r4", "r3"]
    assert [r.run_id for r in page2] == ["r2", "r1"]
    assert [r.run_id for r in page3] == ["r0"]


async def test_list_filters_every_dimension(pg, store):
    await _seed(store, pg, "a", preset="wx", version=1, user="u1", session="s1", at=0)
    await _seed(store, pg, "b", preset="wx", version=2, user="u2", session="s2", at=1)
    await _seed(store, pg, "c", preset="news", version=1, user="u1", session="s2", at=2)
    await store.update_outcome("a", "error", _iso(3), trace_id=None)
    await store.update_outcome("b", "aborted", _iso(4), trace_id=None)

    assert {r.run_id for r in await store.list(RunIndexFilter(preset="wx"), page=1, page_size=10)} == {"a", "b"}
    assert {r.run_id for r in await store.list(RunIndexFilter(version=1), page=1, page_size=10)} == {"a", "c"}
    assert {r.run_id for r in await store.list(RunIndexFilter(user="u1"), page=1, page_size=10)} == {"a", "c"}
    assert {r.run_id for r in await store.list(RunIndexFilter(session="s2"), page=1, page_size=10)} == {"b", "c"}
    assert {r.run_id for r in await store.list(RunIndexFilter(outcome="error"), page=1, page_size=10)} == {"a"}
    assert {r.run_id for r in await store.list(RunIndexFilter(outcome="aborted"), page=1, page_size=10)} == {"b"}
    # Combined filters intersect.
    assert {r.run_id for r in await store.list(RunIndexFilter(preset="wx", user="u1"), page=1, page_size=10)} == {"a"}


async def test_list_time_range_is_inclusive(pg, store):
    for i in range(5):
        await _seed(store, pg, f"r{i}", at=i)
    t0 = _BASE + timedelta(seconds=1)
    t1 = _BASE + timedelta(seconds=3)
    rows = await store.list(RunIndexFilter(t0=t0, t1=t1), page=1, page_size=10)
    assert {r.run_id for r in rows} == {"r1", "r2", "r3"}


async def test_prune_deletes_only_rows_before_cutoff(pg, store):
    for i in range(5):
        await _seed(store, pg, f"r{i}", at=i * 10)
    cutoff = _BASE + timedelta(seconds=25)  # keeps r0,r1,r2 (0/10/20)? no — deletes <25
    deleted = await store.prune(cutoff)
    assert deleted == 3  # r0(0), r1(10), r2(20)
    remaining = {r.run_id for r in await store.list(RunIndexFilter(), page=1, page_size=10)}
    assert remaining == {"r3", "r4"}
