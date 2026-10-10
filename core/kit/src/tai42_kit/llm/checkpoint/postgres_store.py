"""The kit's SQL against a Postgres checkpoint store, behind one module.

The stale-thread query reads the Postgres saver's own ``checkpoints`` table (``checkpoint JSONB``
carrying the checkpoint's ``ts``) joined with the declared waiting retentions; the finished-thread
ledger, the declared retentions and the store-format marker are kit tables beside it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime

    from psycopg_pool import AsyncConnectionPool

FINISHED_TABLE: Final = "tai42_checkpoint_finished"
RETENTION_TABLE: Final = "tai42_checkpoint_retention"
FORMAT_TABLE: Final = "tai42_checkpoint_format"

_STALE_THREADS_SQL: Final = (
    f"SELECT c.thread_id FROM checkpoints c LEFT JOIN {RETENTION_TABLE} r USING (thread_id) "  # noqa: S608 -- constant table name
    "GROUP BY c.thread_id, r.waiting_minutes "
    "HAVING max((c.checkpoint->>'ts')::timestamptz) < "
    "%(now)s - make_interval(mins => COALESCE(r.waiting_minutes, %(default_minutes)s)) "
    "ORDER BY c.thread_id"
)


async def stale_threads(pool: AsyncConnectionPool, *, now: datetime, default_minutes: int) -> list[str]:
    """The threads whose newest checkpoint is older than their waiting retention before ``now``.

    A thread's waiting retention is the one its owner declared at run start, else ``default_minutes``.
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_STALE_THREADS_SQL, {"now": now, "default_minutes": default_minutes})
        rows = await cur.fetchall()
    return [_first(row) for row in rows]


async def holds_checkpoints(pool: AsyncConnectionPool) -> bool:
    """Whether the saver's ``checkpoints`` table holds any row."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT 1 FROM checkpoints LIMIT 1")
        return await cur.fetchone() is not None


async def create_ledger_tables(pool: AsyncConnectionPool) -> None:
    """Create the finished-thread ledger table with its due-time index, and the declared-retention table."""
    async with pool.connection() as conn:
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {FINISHED_TABLE} (thread_id TEXT PRIMARY KEY, due_at TIMESTAMPTZ NOT NULL)"
        )
        await conn.execute(f"CREATE INDEX IF NOT EXISTS {FINISHED_TABLE}_due ON {FINISHED_TABLE} (due_at)")
        await conn.execute(
            f"CREATE TABLE IF NOT EXISTS {RETENTION_TABLE} "
            "(thread_id TEXT PRIMARY KEY, waiting_minutes INTEGER NOT NULL)"
        )


async def start_threads(pool: AsyncConnectionPool, thread_ids: Sequence[str], waiting_minutes: int | None) -> None:
    """Remove the finished marks of ``thread_ids`` and record their declared waiting (``None`` removes it), at once."""
    ids = list(thread_ids)
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(f"DELETE FROM {FINISHED_TABLE} WHERE thread_id = ANY(%s)", (ids,))  # noqa: S608 -- constant table name
        if waiting_minutes is None:
            await cur.execute(f"DELETE FROM {RETENTION_TABLE} WHERE thread_id = ANY(%s)", (ids,))  # noqa: S608 -- constant table name
        else:
            await cur.executemany(
                f"INSERT INTO {RETENTION_TABLE} (thread_id, waiting_minutes) VALUES (%s, %s) "  # noqa: S608 -- constant table name
                "ON CONFLICT (thread_id) DO UPDATE SET waiting_minutes = EXCLUDED.waiting_minutes",
                [(thread_id, waiting_minutes) for thread_id in ids],
            )


async def mark_finished(pool: AsyncConnectionPool, thread_ids: Sequence[str], due_at: datetime) -> None:
    """Record ``thread_ids`` as finished, due at ``due_at``."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.executemany(
            f"INSERT INTO {FINISHED_TABLE} (thread_id, due_at) VALUES (%s, %s) "  # noqa: S608 -- constant table name
            "ON CONFLICT (thread_id) DO UPDATE SET due_at = EXCLUDED.due_at",
            [(thread_id, due_at) for thread_id in thread_ids],
        )


async def finished_before(pool: AsyncConnectionPool, cutoff: datetime, limit: int) -> list[str]:
    """Up to ``limit`` thread ids due at or before ``cutoff``, earliest first."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT thread_id FROM {FINISHED_TABLE} WHERE due_at <= %s "  # noqa: S608 -- constant table name
            "ORDER BY due_at, thread_id LIMIT %s",
            (cutoff, limit),
        )
        rows = await cur.fetchall()
    return [_first(row) for row in rows]


async def forget_threads(pool: AsyncConnectionPool, thread_ids: Sequence[str]) -> None:
    """Remove ``thread_ids`` from the ledger: their finished marks and their declared waiting."""
    ids = list(thread_ids)
    async with pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
        await cur.execute(f"DELETE FROM {FINISHED_TABLE} WHERE thread_id = ANY(%s)", (ids,))  # noqa: S608 -- constant table name
        await cur.execute(f"DELETE FROM {RETENTION_TABLE} WHERE thread_id = ANY(%s)", (ids,))  # noqa: S608 -- constant table name


async def declared_waiting(pool: AsyncConnectionPool, thread_ids: Sequence[str]) -> dict[str, int]:
    """The recorded declared waiting minutes of those of ``thread_ids`` that have one."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            f"SELECT thread_id, waiting_minutes FROM {RETENTION_TABLE} WHERE thread_id = ANY(%s)",  # noqa: S608 -- constant table name
            (list(thread_ids),),
        )
        rows = await cur.fetchall()
    return {_first(row): int(_second(row)) for row in rows}


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


def _second(row: object) -> Any:
    if isinstance(row, dict):
        return list(row.values())[1]
    return row[1]  # type: ignore[index]


def _first(row: object) -> str:
    # The checkpoint pool yields dict rows; a plain connection yields tuples.
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]  # type: ignore[index]
