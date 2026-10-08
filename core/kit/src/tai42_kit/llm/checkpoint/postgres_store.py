"""The kit's SQL against a Postgres checkpoint store, behind one module.

The stale-thread query reads the Postgres saver's own ``checkpoints`` table (``checkpoint JSONB``
carrying the checkpoint's ``ts``); the finished-thread ledger and the store-format marker are kit
tables beside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from psycopg_pool import AsyncConnectionPool

FINISHED_TABLE: Final = "tai42_checkpoint_finished"
FORMAT_TABLE: Final = "tai42_checkpoint_format"

_STALE_THREADS_SQL: Final = (
    "SELECT thread_id FROM checkpoints GROUP BY thread_id "
    "HAVING max((checkpoint->>'ts')::timestamptz) < %(cutoff)s ORDER BY thread_id"
)


async def stale_threads(pool: AsyncConnectionPool, *, cutoff: datetime) -> list[str]:
    """The threads whose newest checkpoint is older than ``cutoff``."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_STALE_THREADS_SQL, {"cutoff": cutoff})
        rows = await cur.fetchall()
    return [_first(row) for row in rows]


async def holds_checkpoints(pool: AsyncConnectionPool) -> bool:
    """Whether the saver's ``checkpoints`` table holds any row."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM checkpoints LIMIT 1")
        return await cur.fetchone() is not None


async def create_finished_table(pool: AsyncConnectionPool) -> None:
    """Create the finished-thread ledger table and its time index."""
    async with pool.connection() as conn:
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {FINISHED_TABLE} "
            "(thread_id TEXT PRIMARY KEY, finished_at TIMESTAMPTZ NOT NULL)"
        )
        await conn.execute(f"CREATE INDEX IF NOT EXISTS {FINISHED_TABLE}_at ON {FINISHED_TABLE} (finished_at)")


async def mark_finished(pool: AsyncConnectionPool, thread_ids: Sequence[str], at: datetime) -> None:
    """Record ``thread_ids`` as finished at ``at``."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            f"INSERT INTO {FINISHED_TABLE} (thread_id, finished_at) VALUES (%s, %s) "  # noqa: S608 -- constant table name
            "ON CONFLICT (thread_id) DO UPDATE SET finished_at = EXCLUDED.finished_at",
            [(thread_id, at) for thread_id in thread_ids],
        )


async def finished_before(pool: AsyncConnectionPool, cutoff: datetime, limit: int) -> list[str]:
    """Up to ``limit`` thread ids marked at or before ``cutoff``, oldest first."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT thread_id FROM {FINISHED_TABLE} WHERE finished_at <= %s "  # noqa: S608 -- constant table name
            "ORDER BY finished_at, thread_id LIMIT %s",
            (cutoff, limit),
        )
        rows = await cur.fetchall()
    return [_first(row) for row in rows]


async def forget_finished(pool: AsyncConnectionPool, thread_ids: Sequence[str]) -> None:
    """Remove ``thread_ids`` from the ledger."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"DELETE FROM {FINISHED_TABLE} WHERE thread_id = ANY(%s)",  # noqa: S608 -- constant table name
            (list(thread_ids),),
        )


async def read_format_generation(pool: AsyncConnectionPool) -> int | None:
    """The store-format generation the marker table records, or ``None`` when none is recorded."""
    async with pool.connection() as conn:
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {FORMAT_TABLE} "
            "(id SMALLINT PRIMARY KEY CHECK (id = 1), generation INTEGER NOT NULL)"
        )
        async with conn.cursor() as cur:
            await cur.execute(f"SELECT generation FROM {FORMAT_TABLE} WHERE id = 1")  # noqa: S608 -- constant table name
            row = await cur.fetchone()
    return None if row is None else int(_first(row))


async def write_format_generation(pool: AsyncConnectionPool, generation: int) -> None:
    """Record ``generation`` unless a generation is already recorded."""
    async with pool.connection() as conn:
        await conn.execute(
            f"INSERT INTO {FORMAT_TABLE} (id, generation) VALUES (1, %s) ON CONFLICT (id) DO NOTHING",  # noqa: S608 -- constant table name
            (generation,),
        )


def _first(row: object) -> str:
    # The checkpoint pool yields dict rows; a plain connection yields tuples.
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]  # type: ignore[index]
