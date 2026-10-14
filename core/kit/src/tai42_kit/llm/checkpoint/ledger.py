"""The finished-thread ledger: one checkpoint store's per-thread retention record, on both horizons.

A thread's owner — the code that minted its id — marks it finished at its terminal, with the time it
becomes deletable: its finished retention (the owner's declared one, else the platform's) after the
mark; the checkpoint sweep deletes it at or after that due time. Every consumer that starts a run on
a thread marks it active first (removes it from the ledger), so a thread used again never sits in
the ledger while it is live. Where the store's saver does not carry the waiting retention as a key
TTL, starting a run also records the owner's declared waiting retention, and the ledger lists the
threads past their waiting horizon for the sweep. The ledger lives in the same store as the
checkpoints, so a store reset clears both.
"""

from __future__ import annotations

from contextlib import aclosing
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Protocol, cast

from tai42_kit.llm.checkpoint.retention import ThreadRetention, platform_retention
from tai42_kit.llm.settings import llm_provider_settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence

    from langgraph.checkpoint.base import BaseCheckpointSaver
    from psycopg_pool import AsyncConnectionPool
    from redis.asyncio import Redis as AsyncRedis

# The Redis sorted set of finished threads (member = thread id, score = epoch seconds of the due time).
REDIS_FINISHED_THREADS_KEY: Final = "tai42:checkpoint:finished"

# The SQL table of finished threads (Postgres and sqlite).
FINISHED_THREADS_TABLE: Final = "tai42_checkpoint_finished"

# The SQL table of the waiting retention an owner declared for a thread (Postgres and sqlite).
RETENTION_TABLE: Final = "tai42_checkpoint_retention"

# Thread ids per declared-waiting lookup of the generic stale-thread walk (sqlite binds one
# placeholder per id).
_DECLARED_WAITING_PAGE: Final = 1000

_SQLITE_SETUP: Final = (
    f"CREATE TABLE IF NOT EXISTS {FINISHED_THREADS_TABLE} (thread_id TEXT PRIMARY KEY, due_at TEXT NOT NULL)",
    f"CREATE INDEX IF NOT EXISTS {FINISHED_THREADS_TABLE}_due ON {FINISHED_THREADS_TABLE} (due_at)",
    f"CREATE TABLE IF NOT EXISTS {RETENTION_TABLE} (thread_id TEXT PRIMARY KEY, waiting_minutes INTEGER NOT NULL)",
)


class FinishedThreadLedger(Protocol):
    """The per-thread retention record of one checkpoint store.

    It holds the finished marks and their due times, the declared waiting retentions, and lists the
    threads past their waiting horizon. Every method that takes thread ids takes any sequence; an
    empty one changes nothing, on every provider.
    """

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        """A run starts on ``thread_ids``: remove their finished mark and record their declared waiting.

        The waiting is recorded wherever the store's saver does not carry it as a key TTL; ``None``
        (the platform's) removes a recorded one. A store whose saver stamps the waiting as the TTL of
        every key it writes records nothing.
        """
        ...

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        """Record ``thread_ids`` as finished and deletable at or after ``due_at`` (a later mark moves it)."""
        ...

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids due at or before ``cutoff``, earliest due first."""
        ...

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger: their finished mark and their declared waiting."""
        ...

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        """The recorded declared waiting minutes of those of ``thread_ids`` that have one."""
        ...

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """The threads whose newest checkpoint is older than their waiting retention before ``now``, by id.

        A thread's waiting retention is the one its owner declared at run start, else
        ``default_minutes``; its newest checkpoint is the newest in any of its namespaces. A store with
        an index of its own answers from it; otherwise ``saver`` (the store's own) is walked. ``now``
        must be timezone-aware.
        """
        ...


def _utc(at: datetime) -> datetime:
    if at.tzinfo is None:
        raise ValueError("a finished-thread ledger time must be timezone-aware")
    return at.astimezone(UTC)


async def stale_threads_by_walk(
    saver: BaseCheckpointSaver,
    declared_waiting: Callable[[Sequence[str]], Awaitable[dict[str, int]]],
    *,
    now: datetime,
    default_minutes: int,
) -> list[str]:
    """The stale-thread listing of a store without an index of its own, by id.

    Reads the newest checkpoint ``ts`` of every thread, in every namespace, through
    ``saver.alist(None)``, and compares it with the thread's ``declared_waiting`` (looked up page by
    page), else ``default_minutes``, before ``now``.
    """
    bound = _utc(now)
    newest_by_thread: dict[str, datetime] = {}
    every_tuple = cast("AsyncGenerator[Any]", saver.alist(None))
    async with aclosing(every_tuple) as tuples:
        async for tup in tuples:
            thread_id = tup.config["configurable"]["thread_id"]
            ts = datetime.fromisoformat(tup.checkpoint["ts"])
            current = newest_by_thread.get(thread_id)
            if current is None or ts > current:
                newest_by_thread[thread_id] = ts
    stale: list[str] = []
    thread_ids = sorted(newest_by_thread)
    for start in range(0, len(thread_ids), _DECLARED_WAITING_PAGE):
        page = thread_ids[start : start + _DECLARED_WAITING_PAGE]
        declared = await declared_waiting(page)
        stale.extend(
            thread_id
            for thread_id in page
            if newest_by_thread[thread_id] < bound - timedelta(minutes=declared.get(thread_id, default_minutes))
        )
    return stale


def _sqlite_time(at: datetime) -> str:
    # One fixed width, so the stored text orders exactly as the times do.
    return _utc(at).isoformat(timespec="microseconds")


class MemoryFinishedThreadLedger:
    """The ledger of an in-process (``memory``) checkpoint store."""

    def __init__(self) -> None:
        """Start empty; the ledger lives as long as its store."""
        self._finished: dict[str, datetime] = {}
        self._waiting: dict[str, int] = {}

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        """Remove the finished marks of ``thread_ids`` and record (or, for ``None``, remove) their waiting."""
        for thread_id in thread_ids:
            self._finished.pop(thread_id, None)
            if retention is None:
                self._waiting.pop(thread_id, None)
            else:
                self._waiting[thread_id] = retention.waiting_minutes

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        """Record ``thread_ids`` as finished, due at ``due_at``."""
        when = _utc(due_at)
        for thread_id in thread_ids:
            self._finished[thread_id] = when

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids due at or before ``cutoff``, earliest first."""
        bound = _utc(cutoff)
        due = sorted((when, thread_id) for thread_id, when in self._finished.items() if when <= bound)
        return [thread_id for _, thread_id in due[:limit]]

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        for thread_id in thread_ids:
            self._finished.pop(thread_id, None)
            self._waiting.pop(thread_id, None)

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        """The recorded declared waiting of those of ``thread_ids`` that have one."""
        return {thread_id: self._waiting[thread_id] for thread_id in thread_ids if thread_id in self._waiting}

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """The threads past their waiting horizon, walked through ``saver``."""
        return await stale_threads_by_walk(saver, self.declared_waiting, now=now, default_minutes=default_minutes)


class SqliteFinishedThreadLedger:
    """The ledger of a sqlite checkpoint store, two tables on its shared connection."""

    def __init__(self, conn: Any) -> None:
        """``conn``: the store's aiosqlite connection."""
        self._conn = conn

    async def setup(self) -> None:
        """Create the ledger tables beside the saver's tables."""
        for statement in _SQLITE_SETUP:
            await self._conn.execute(statement)
        await self._conn.commit()

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        """Remove the finished marks of ``thread_ids`` and record (or, for ``None``, remove) their waiting."""
        rows = [(thread_id,) for thread_id in thread_ids]
        await self._conn.executemany(
            f"DELETE FROM {FINISHED_THREADS_TABLE} WHERE thread_id = ?",  # noqa: S608 -- constant table name
            rows,
        )
        if retention is None:
            await self._conn.executemany(
                f"DELETE FROM {RETENTION_TABLE} WHERE thread_id = ?",  # noqa: S608 -- constant table name
                rows,
            )
        else:
            await self._conn.executemany(
                f"INSERT INTO {RETENTION_TABLE} (thread_id, waiting_minutes) VALUES (?, ?) "  # noqa: S608 -- constant table name
                "ON CONFLICT (thread_id) DO UPDATE SET waiting_minutes = excluded.waiting_minutes",
                [(thread_id, retention.waiting_minutes) for thread_id in thread_ids],
            )
        await self._conn.commit()

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        """Record ``thread_ids`` as finished, due at ``due_at``."""
        when = _sqlite_time(due_at)
        await self._conn.executemany(
            f"INSERT INTO {FINISHED_THREADS_TABLE} (thread_id, due_at) VALUES (?, ?) "  # noqa: S608 -- constant table name
            "ON CONFLICT (thread_id) DO UPDATE SET due_at = excluded.due_at",
            [(thread_id, when) for thread_id in thread_ids],
        )
        await self._conn.commit()

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids due at or before ``cutoff``, earliest first."""
        async with self._conn.execute(
            f"SELECT thread_id FROM {FINISHED_THREADS_TABLE} WHERE due_at <= ? "  # noqa: S608 -- constant table name
            "ORDER BY due_at, thread_id LIMIT ?",
            (_sqlite_time(cutoff), limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        rows = [(thread_id,) for thread_id in thread_ids]
        for table in (FINISHED_THREADS_TABLE, RETENTION_TABLE):
            await self._conn.executemany(f"DELETE FROM {table} WHERE thread_id = ?", rows)  # noqa: S608 -- constant table name
        await self._conn.commit()

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        """The recorded declared waiting of those of ``thread_ids`` that have one."""
        ids = list(thread_ids)
        if not ids:
            return {}
        placeholders = ", ".join("?" for _ in ids)
        async with self._conn.execute(
            f"SELECT thread_id, waiting_minutes FROM {RETENTION_TABLE} WHERE thread_id IN ({placeholders})",  # noqa: S608 -- constant table name, bound values
            ids,
        ) as cursor:
            rows = await cursor.fetchall()
        return {row[0]: int(row[1]) for row in rows}

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """The threads past their waiting horizon, walked through ``saver``."""
        return await stale_threads_by_walk(saver, self.declared_waiting, now=now, default_minutes=default_minutes)


class PostgresFinishedThreadLedger:
    """The ledger of a Postgres checkpoint store, two tables beside the saver's tables."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        """``pool``: the store's psycopg pool."""
        self._pool = pool

    async def setup(self) -> None:
        """Create the ledger tables beside the saver's tables."""
        from tai42_kit.llm.checkpoint import postgres_store

        await postgres_store.create_ledger_tables(self._pool)

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        """Remove the finished marks of ``thread_ids`` and record (or, for ``None``, remove) their waiting."""
        from tai42_kit.llm.checkpoint import postgres_store

        await postgres_store.start_threads(
            self._pool, thread_ids, None if retention is None else retention.waiting_minutes
        )

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        """Record ``thread_ids`` as finished, due at ``due_at``."""
        from tai42_kit.llm.checkpoint import postgres_store

        await postgres_store.mark_finished(self._pool, thread_ids, _utc(due_at))

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids due at or before ``cutoff``, earliest first."""
        from tai42_kit.llm.checkpoint import postgres_store

        return await postgres_store.finished_before(self._pool, _utc(cutoff), limit)

    async def forget(self, thread_ids: Sequence[str]) -> None:
        """Remove ``thread_ids`` from the ledger."""
        from tai42_kit.llm.checkpoint import postgres_store

        await postgres_store.forget_threads(self._pool, thread_ids)

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        """The recorded declared waiting of those of ``thread_ids`` that have one."""
        from tai42_kit.llm.checkpoint import postgres_store

        return await postgres_store.declared_waiting(self._pool, thread_ids)

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """The threads past their waiting horizon, from one query over the store's own tables; ``saver`` is unused."""
        from tai42_kit.llm.checkpoint import postgres_store

        return await postgres_store.stale_threads(self._pool, now=_utc(now), default_minutes=default_minutes)


class RedisFinishedThreadLedger:
    """The ledger of a Redis checkpoint store: a sorted set on the kit's own client.

    A thread's waiting retention is the TTL its saver stamps on every key it writes, so ``start``
    records no declaration and ``declared_waiting`` knows none. Each mark re-arms the set's expiry to
    the platform's waiting retention, the longest any thread lives: the threads it names expire on
    their own key TTL within that horizon, so with no sweep ever scheduled the set outlives its
    newest mark by at most that horizon.
    """

    def __init__(self, client: AsyncRedis) -> None:
        """``client``: the Redis client the kit built for the checkpoint saver."""
        self._client = client

    async def start(self, thread_ids: Sequence[str], retention: ThreadRetention | None) -> None:
        """Remove the finished marks of ``thread_ids``."""
        await self.forget(thread_ids)

    async def mark(self, thread_ids: Sequence[str], due_at: datetime) -> None:
        """Record ``thread_ids`` as finished, due at ``due_at``."""
        score = _utc(due_at).timestamp()
        if not thread_ids:
            # ZADD takes at least one member.
            return
        ttl_seconds = llm_provider_settings().checkpoint_retention_waiting_minutes * 60
        pipeline = self._client.pipeline(transaction=True)
        pipeline.zadd(REDIS_FINISHED_THREADS_KEY, dict.fromkeys(thread_ids, score))
        pipeline.expire(REDIS_FINISHED_THREADS_KEY, ttl_seconds)
        await pipeline.execute()

    async def finished_before(self, cutoff: datetime, limit: int) -> list[str]:
        """Up to ``limit`` thread ids due at or before ``cutoff``, earliest first."""
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

    async def declared_waiting(self, thread_ids: Sequence[str]) -> dict[str, int]:
        """Always empty: the waiting retention lives on the thread's own keys."""
        return {}

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """Always empty: every key a write stamps carries the thread's waiting retention as its TTL.

        So no thread past its waiting horizon remains in this store.
        """
        _utc(now)
        return []


async def _ledger(provider: str | None, conn_string: str | None) -> FinishedThreadLedger:
    from tai42_kit.llm.checkpoint.checkpoint_registry import checkpoint_registry

    settings = llm_provider_settings()
    resolved_provider = settings.checkpoint if provider is None else provider
    resolved_conn = settings.checkpoint_conn_string if conn_string is None else conn_string
    return await checkpoint_registry().ledger(resolved_provider, resolved_conn)


async def mark_threads_finished(
    thread_ids: Sequence[str],
    *,
    retention: ThreadRetention | None = None,
    provider: str | None = None,
    conn_string: str | None = None,
) -> None:
    """Mark ``thread_ids`` finished now, in the ledger of the store that holds them.

    They become deletable their finished retention after now: ``retention``'s (the owner's
    declaration, see :func:`~tai42_kit.llm.checkpoint.retention.resolve_retention`), else the
    platform's. ``provider`` and ``conn_string`` resolve independently: ``None`` is the deployment's
    configured checkpoint provider, respectively its configured connection string. A caller that
    checkpoints on a per-run provider passes it. An empty sequence is a no-op; a store error
    propagates.
    """
    if not thread_ids:
        return
    finished_minutes = (retention or platform_retention()).finished_minutes
    ledger = await _ledger(provider, conn_string)
    await ledger.mark(list(thread_ids), datetime.now(UTC) + timedelta(minutes=finished_minutes))


async def mark_threads_active(
    thread_ids: Sequence[str],
    *,
    retention: ThreadRetention | None = None,
    provider: str | None = None,
    conn_string: str | None = None,
) -> None:
    """Take ``thread_ids`` out of the ledger before a run starts on them, recording ``retention``'s waiting.

    ``retention`` is the owner's declaration for the threads (``None``: the platform's); the store
    records its waiting where the waiting horizon is swept. ``provider`` and ``conn_string`` resolve
    as in :func:`mark_threads_finished`.
    """
    if not thread_ids:
        return
    ledger = await _ledger(provider, conn_string)
    await ledger.start(list(thread_ids), retention)
