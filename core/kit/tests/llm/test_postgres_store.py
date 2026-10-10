"""The kit's SQL against a real Postgres checkpoint store, on the saver's own ``setup()`` schema.

The stale-thread query reads the Postgres saver's ``checkpoints`` table; a schema change in the
saver turns this suite red. Opt-in: ``TAI42_KIT_REAL_PG=1`` and the ``TAI_DATABASE_DEFAULT_PG_*`` env.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

pytest.importorskip("langgraph.checkpoint.postgres.aio")

from langgraph.checkpoint.base import empty_checkpoint

from tai42_kit.llm.checkpoint import postgres_store

pytestmark = pytest.mark.integration


@pytest.fixture
async def saver_and_pool() -> AsyncIterator[tuple[Any, Any]]:
    if os.environ.get("TAI42_KIT_REAL_PG") not in ("1", "true", "True"):
        pytest.skip("real-Postgres checkpoint store test is opt-in: set TAI42_KIT_REAL_PG=1 and the PG env")
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    from tai42_kit.clients.impl.postgres import open_dedicated_pool
    from tai42_kit.db import database_settings
    from tai42_kit.llm._langgraph_postgres import LANGGRAPH_CONNECTION_KWARGS

    pool = await open_dedicated_pool(
        database_settings("default").pg_dsn, role="test", connection_kwargs=dict(LANGGRAPH_CONNECTION_KWARGS)
    )
    try:
        saver = AsyncPostgresSaver(cast("Any", pool))
        await saver.setup()
        await postgres_store.create_ledger_tables(pool)
        await _clear(pool)
        yield saver, pool
        await _clear(pool)
    finally:
        await pool.close()


async def _clear(pool: Any) -> None:
    # The suite shares one database with the kit's other checkpoint integration tests.
    async with pool.connection() as conn:
        await conn.execute("DELETE FROM checkpoint_writes")
        await conn.execute("DELETE FROM checkpoint_blobs")
        await conn.execute("DELETE FROM checkpoints")
        await conn.execute(f"DELETE FROM {postgres_store.RETENTION_TABLE}")  # noqa: S608 -- constant table name


async def _put(saver: Any, thread_id: str, ts: datetime) -> None:
    checkpoint = empty_checkpoint()
    checkpoint["ts"] = ts.isoformat()
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    await saver.aput(config, checkpoint, {"source": "input", "step": 0, "parents": {}}, {})


async def test_stale_threads_are_those_whose_newest_checkpoint_is_older_than_the_cutoff(saver_and_pool):
    saver, pool = saver_and_pool
    now = datetime.now(UTC)
    stale = f"stale-{uuid.uuid4().hex}"
    fresh = f"fresh-{uuid.uuid4().hex}"
    revived = f"revived-{uuid.uuid4().hex}"
    await _put(saver, stale, now - timedelta(hours=5))
    await _put(saver, stale, now - timedelta(hours=4))
    await _put(saver, fresh, now - timedelta(minutes=5))
    await _put(saver, revived, now - timedelta(hours=6))
    await _put(saver, revived, now - timedelta(minutes=1))
    assert await postgres_store.stale_threads(pool, now=now, default_minutes=60) == [stale]


async def _declare(pool: Any, thread_id: str, waiting_minutes: int) -> None:
    from tai42_kit.llm.checkpoint import ThreadRetention
    from tai42_kit.llm.checkpoint.ledger import PostgresFinishedThreadLedger

    await PostgresFinishedThreadLedger(pool).start([thread_id], ThreadRetention(waiting_minutes, 1))


async def test_a_declared_waiting_is_each_thread_own_stale_horizon(saver_and_pool):
    saver, pool = saver_and_pool
    now = datetime.now(UTC)
    declared_short = f"short-{uuid.uuid4().hex}"
    undeclared = f"undeclared-{uuid.uuid4().hex}"
    declared_long = f"long-{uuid.uuid4().hex}"
    await _put(saver, declared_short, now - timedelta(minutes=3))
    await _put(saver, undeclared, now - timedelta(minutes=3))
    await _put(saver, declared_long, now - timedelta(minutes=45))
    await _declare(pool, declared_short, 2)
    await _declare(pool, declared_long, 60)
    assert await postgres_store.stale_threads(pool, now=now, default_minutes=60) == [declared_short]
    assert await postgres_store.stale_threads(pool, now=now, default_minutes=30) == [declared_short]
    assert sorted(await postgres_store.stale_threads(pool, now=now, default_minutes=1)) == sorted(
        [declared_short, undeclared]
    )


async def test_holds_checkpoints(saver_and_pool):
    saver, pool = saver_and_pool
    assert await postgres_store.holds_checkpoints(pool) is False
    await _put(saver, "t", datetime.now(UTC))
    assert await postgres_store.holds_checkpoints(pool) is True


async def test_format_generation_is_written_once_and_read_back(saver_and_pool):
    _saver, pool = saver_and_pool
    async with pool.connection() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {postgres_store.FORMAT_TABLE}")
    assert await postgres_store.read_format_generation(pool) is None
    await postgres_store.write_format_generation(pool, 2)
    await postgres_store.write_format_generation(pool, 3)  # a recorded generation is kept
    assert await postgres_store.read_format_generation(pool) == 2
    async with pool.connection() as conn:
        await conn.execute(f"DROP TABLE {postgres_store.FORMAT_TABLE}")


def test_first_column_reads_dict_and_tuple_rows():
    assert postgres_store._first({"thread_id": "a"}) == "a"
    assert postgres_store._first(("b",)) == "b"


def test_second_column_reads_dict_and_tuple_rows():
    assert postgres_store._second({"thread_id": "a", "waiting_minutes": 5}) == 5
    assert postgres_store._second(("b", 7)) == 7
