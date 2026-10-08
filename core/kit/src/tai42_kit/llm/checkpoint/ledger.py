"""The finished-thread ledger: which checkpoint threads their owner marked finished, and when.

A thread's owner — the code that minted its id — marks it finished at its terminal; the checkpoint
sweep deletes it ``checkpoint_retention_finished_minutes`` later. Every consumer that starts a run on
a thread marks it active first (removes it from the ledger), so a thread used again never sits in
the ledger while it is live. The ledger lives in the same store as the checkpoints, so a store reset
clears both.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from tai42_kit.llm.settings import llm_provider_settings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from psycopg_pool import AsyncConnectionPool
    from redis.asyncio import Redis as AsyncRedis

# The Redis sorted set of finished threads (member = thread id, score = epoch seconds of the mark).
REDIS_FINISHED_THREADS_KEY: Final = "tai42:checkpoint:finished"

# The SQL table of finished threads (Postgres and sqlite).
FINISHED_THREADS_TABLE: Final = "tai42_checkpoint_finished"

_SQLITE_SETUP: Final = (
    f"CREATE TABLE IF NOT EXISTS {FINISHED_THREADS_TABLE} (thread_id TEXT PRIMARY KEY, finished_at TEXT NOT NULL)",
    f"CREATE INDEX IF NOT EXISTS {FINISHED_THREADS_TABLE}_at ON {FINISHED_THREADS_TABLE} (finished_at)",
)


class FinishedThreadLedger(Protocol):
    """The finished-thread ledger of one checkpoint store.

    ``mark`` and ``forget`` take any sequence of thread ids; an empty one changes nothing, on every provider.
    """

    async def mark(self, thread_ids: Sequence[str], at: datetime) -> None:
        """Record ``thread_ids`` as finished at ``at`` (a later mark moves the time)."""
        ...

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids marked finished at or before ``cutoff``, oldest first."""
        ...

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        ...


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("a finished-thread ledger time must be timezone-aware")
    return at.astimezone(UTC)


def _sqlite_time(at: datetime) -> str:
    # One fixed width, so the stored text orders exactly as the times do.
    return _utc(at).isoformat(timespec="microseconds")


class MemoryFinishedThreadLedger:
    """The ledger of an in-process (``memory``) checkpoint store."""

    def __init__(self) -> None:
        """Start empty; the ledger lives as long as its store."""
        self._finished: dict[str, datetime] = {}

    async def mark(self, thread_ids: Sequence[str], at: datetime) -> None:
        """Record ``thread_ids`` as finished at ``at``."""
        when = _utc(at)
        for thread_id in thread_ids:
            self._finished[thread_id] = when

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids marked at or before ``cutoff``, oldest first."""
        bound = _utc(cutoff)
        due = sorted((when, thread_id) for thread_id, when in self._finished.items() if when <= bound)
        return [thread_id for _, thread_id in due[:limit]]

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        for thread_id in thread_ids:
            self._finished.pop(thread_id, None)


class SqliteFinishedThreadLedger:
    """The ledger of a sqlite checkpoint store, a table on its shared connection."""

    def __init__(self, conn: Any) -> None:
        """``conn``: the store's aiosqlite connection."""
        self._conn = conn

    async def setup(self) -> None:
        """Create the ledger table beside the saver's tables."""
        for statement in _SQLITE_SETUP:
            await self._conn.execute(statement)
        await self._conn.commit()

    async def mark(self, thread_ids: Sequence[str], at: datetime) -> None:
        """Record ``thread_ids`` as finished at ``at``."""
        when = _sqlite_time(at)
        await self._conn.executemany(
            f"INSERT INTO {FINISHED_THREADS_TABLE} (thread_id, finished_at) VALUES (?, ?) "  # noqa: S608 -- constant table name
            "ON CONFLICT (thread_id) DO UPDATE SET finished_at = excluded.finished_at",
            [(thread_id, when) for thread_id in thread_ids],
        )
        await self._conn.commit()

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids marked at or before ``cutoff``, oldest first."""
        async with self._conn.execute(
            f"SELECT thread_id FROM {FINISHED_THREADS_TABLE} WHERE finished_at <= ? "  # noqa: S608 -- constant table name
            "ORDER BY finished_at, thread_id LIMIT ?",
            (_sqlite_time(cutoff), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        await self._conn.executemany(
            f"DELETE FROM {FINISHED_THREADS_TABLE} WHERE thread_id = ?",  # noqa: S608 -- constant table name
            [(thread_id,) for thread_id in thread_ids],
        )
        await self._conn.commit()


class PostgresFinishedThreadLedger:
    """The ledger of a Postgres checkpoint store, a table beside the saver's tables."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """``pool``: the store's psycopg pool."""
        self._pool = pool

    async def setup(self) -> None:
        """Create the ledger table beside the saver's tables."""
        from tai42_kit.llm.checkpoint import postgres_store

        await postgres_store.create_finished_table(self._pool)

    async def mark(self, thread_ids: Sequence[str], at: datetime) -> None:
        """Record ``thread_ids`` as finished at ``at``."""
        from tai42_kit.llm.checkpoint import postgres_store

        await postgres_store.mark_finished(self._pool, thread_ids, _utc(at))

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids marked at or before ``cutoff``, oldest first."""
        from tai42_kit.llm.checkpoint import postgres_store

        return await postgres_store.finished_before(self._pool, _utc(cutoff), limit)

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        from tai42_kit.llm.checkpoint import postgres_store

        await postgres_store.forget_finished(self._pool, thread_ids)


class RedisFinishedThreadLedger:
    """The ledger of a Redis checkpoint store: a sorted set on the kit's own client.

    Each mark re-arms the set's expiry to the waiting retention: the threads it names expire on
    their own key TTL within that horizon, so with no sweep ever scheduled the set outlives its
    newest mark by exactly that horizon and no longer.
    """

    def __init__(self, client: AsyncRedis) -> None:
        """``client``: the Redis client the kit built for the checkpoint saver."""
        self._client = client

    async def mark(self, thread_ids: Sequence[str], at: datetime) -> None:
        """Record ``thread_ids`` as finished at ``at``."""
        score = _utc(at).timestamp()
        if not thread_ids:
            # ZADD takes at least one member.
            return
        ttl_seconds = llm_provider_settings().checkpoint_retention_waiting_minutes * 60
        pipeline = self._client.pipeline(transaction=True)
        pipeline.zadd(REDIS_FINISHED_THREADS_KEY, dict.fromkeys(thread_ids, score))
        pipeline.expire(REDIS_FINISHED_THREADS_KEY, ttl_seconds)
        await pipeline.execute()

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids marked at or before ``cutoff``, oldest first."""
        members = await self._client.zrangebyscore(
            REDIS_FINISHED_THREADS_KEY, "-inf", _utc(cutoff).timestamp(), start=0, num=limit
        )
        return [member.decode() if isinstance(member, bytes) else member for member in members]

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        if not thread_ids:
            # ZREM takes at least one member.
            return
        await self._client.zrem(REDIS_FINISHED_THREADS_KEY, *thread_ids)


async def _ledger(provider: str | None, conn_string: str | None) -> FinishedThreadLedger:
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry

    settings = llm_provider_settings()
    resolved_provider = settings.checkpoint if provider is None else provider
    resolved_conn = settings.checkpoint_conn_string if conn_string is None else conn_string
    return await checkpoint_registry().ledger(resolved_provider, resolved_conn)


async def mark_threads_finished(
    thread_ids: Sequence[str], *, provider: str | None = None, conn_string: str | None = None
) -> None:
    """Mark ``thread_ids`` finished now, in the ledger of the store that holds them.

    ``provider`` and ``conn_string`` resolve independently: ``None`` is the deployment's
    configured checkpoint provider, respectively its configured connection string. A caller
    that checkpoints on a per-run provider passes it. An empty sequence is a no-op; a store
    error propagates.
    """
    if not thread_ids:
        return
    ledger = await _ledger(provider, conn_string)
    await ledger.mark(list(thread_ids), datetime.now(UTC))


async def mark_threads_active(
    thread_ids: Sequence[str], *, provider: str | None = None, conn_string: str | None = None
) -> None:
    """Remove ``thread_ids`` from the ledger before a run starts on them.

    ``provider`` and ``conn_string`` resolve as in :func:`mark_threads_finished`.
    """
    if not thread_ids:
        return
    ledger = await _ledger(provider, conn_string)
    await ledger.forget(list(thread_ids))
