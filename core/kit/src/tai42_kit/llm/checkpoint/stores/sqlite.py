"""The ``sqlite`` checkpoint store: LangGraph's async sqlite saver on one shared connection, for development.

The connection is shared across the whole application, so the store is not ready for production
concurrency. Beside the saver's own tables the kit keeps its finished-thread ledger, its value table
and its store-format marker in the same file.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar, Final

from tai42_kit.llm.checkpoint.ledger import as_utc, stale_threads_by_walk
from tai42_kit.llm.checkpoint.providers import (
    CheckpointProvider,
    CheckpointProviderFacts,
    register_checkpoint_provider,
)
from tai42_kit.llm.checkpoint.value_store import StoredBlob

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
    from datetime import datetime

    from langgraph.checkpoint.base import BaseCheckpointSaver

    from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource
    from tai42_kit.llm.checkpoint.retention import ThreadRetention
    from tai42_kit.llm.checkpoint.value_encoding import MsgpackValueEncoding

FINISHED_THREADS_TABLE: Final = "tai42_checkpoint_finished"
RETENTION_TABLE: Final = "tai42_checkpoint_retention"
VALUES_TABLE: Final = "tai42_checkpoint_values"
FORMAT_TABLE: Final = "tai42_checkpoint_format"

_LEDGER_SETUP: Final = (
    f"CREATE TABLE IF NOT EXISTS {FINISHED_THREADS_TABLE} (thread_id TEXT PRIMARY KEY, due_at TEXT NOT NULL)",
    f"CREATE INDEX IF NOT EXISTS {FINISHED_THREADS_TABLE}_due ON {FINISHED_THREADS_TABLE} (due_at)",
    f"CREATE TABLE IF NOT EXISTS {RETENTION_TABLE} (thread_id TEXT PRIMARY KEY, waiting_minutes INTEGER NOT NULL)",
)
_VALUES_SETUP: Final = (
    f"CREATE TABLE IF NOT EXISTS {VALUES_TABLE} (thread_id TEXT NOT NULL, digest BLOB NOT NULL, type TEXT NOT NULL, "
    "blob BLOB NOT NULL, PRIMARY KEY (thread_id, digest))"
)
_FORMAT_SETUP: Final = (
    f"CREATE TABLE IF NOT EXISTS {FORMAT_TABLE} (id SMALLINT PRIMARY KEY CHECK (id = 1), generation INTEGER NOT NULL)"
)


def _sqlite_time(at: datetime) -> str:
    # One fixed width, so the stored text orders exactly as the times do.
    return as_utc(at).isoformat(timespec="microseconds")


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


class SqliteFinishedThreadLedger:
    """The ledger of a sqlite checkpoint store, two tables on its shared connection."""

    def __init__(self, conn: Any) -> None:
        """``conn``: the store's aiosqlite connection."""
        self._conn = conn

    async def setup(self) -> None:
        """Create the ledger tables beside the saver's tables."""
        for statement in _LEDGER_SETUP:
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
        async with self._conn.execute(
            f"SELECT thread_id, waiting_minutes FROM {RETENTION_TABLE} WHERE thread_id IN ({_placeholders(len(ids))})",  # noqa: S608 -- constant table name, bound values
            ids,
        ) as cursor:
            rows = await cursor.fetchall()
        return {row[0]: int(row[1]) for row in rows}

    async def stale_threads(self, saver: BaseCheckpointSaver, *, now: datetime, default_minutes: int) -> list[str]:
        """The threads past their waiting horizon, walked through ``saver``."""
        return await stale_threads_by_walk(saver, self.declared_waiting, now=now, default_minutes=default_minutes)


class SqliteCheckpointValueStore:
    """Shared values as rows of the value table in the store's file, deleted with their thread."""

    def __init__(self, conn: Any) -> None:
        """``conn``: the store's aiosqlite connection."""
        self._conn = conn

    async def setup(self) -> None:
        """Create the value table beside the saver's tables."""
        await self._conn.execute(_VALUES_SETUP)
        await self._conn.commit()

    async def _held(self, thread_id: str, digests: Collection[str]) -> list[tuple[str, str, bytes]]:
        ordered = list(digests)
        rows: list[tuple[str, str, bytes]] = []
        # One placeholder per digest, a page at a time.
        for start in range(0, len(ordered), 500):
            page = [bytes.fromhex(digest) for digest in ordered[start : start + 500]]
            async with self._conn.execute(
                f"SELECT digest, type, blob FROM {VALUES_TABLE} WHERE thread_id = ? "  # noqa: S608 -- constant table name, bound values
                f"AND digest IN ({_placeholders(len(page))})",
                (thread_id, *page),
            ) as cursor:
                rows.extend((bytes(row[0]).hex(), str(row[1]), bytes(row[2])) for row in await cursor.fetchall())
        return rows

    async def touch(self, thread_id: str, digests: Collection[str]) -> set[str]:
        """The digests the table does not hold for ``thread_id`` (a row has no retention to re-arm)."""
        wanted = set(digests)
        return wanted - {digest for digest, _type, _blob in await self._held(thread_id, wanted)}

    async def put(self, thread_id: str, blobs: Mapping[str, StoredBlob]) -> None:
        """Insert every blob; a digest already held keeps its row."""
        if not blobs:
            return
        await self._conn.executemany(
            f"INSERT INTO {VALUES_TABLE} (thread_id, digest, type, blob) VALUES (?, ?, ?, ?) "  # noqa: S608 -- constant table name
            "ON CONFLICT (thread_id, digest) DO NOTHING",
            [(thread_id, bytes.fromhex(digest), blob.type, blob.data) for digest, blob in blobs.items()],
        )
        await self._conn.commit()

    async def get(self, thread_id: str, digests: Collection[str]) -> dict[str, StoredBlob]:
        """The held blobs among ``digests``."""
        return {digest: StoredBlob(type_, data) for digest, type_, data in await self._held(thread_id, digests)}

    async def held_digests(self, thread_id: str) -> set[str]:
        """Every digest ``thread_id`` holds."""
        async with self._conn.execute(
            f"SELECT digest FROM {VALUES_TABLE} WHERE thread_id = ?",  # noqa: S608 -- constant table name
            (thread_id,),
        ) as cursor:
            return {bytes(row[0]).hex() for row in await cursor.fetchall()}

    async def delete_thread(self, thread_id: str) -> None:
        """Delete every row of ``thread_id``."""
        await self._conn.execute(f"DELETE FROM {VALUES_TABLE} WHERE thread_id = ?", (thread_id,))  # noqa: S608 -- constant table name
        await self._conn.commit()


@register_checkpoint_provider
class SqliteCheckpointProvider(CheckpointProvider):
    """LangGraph's ``AsyncSqliteSaver`` over one file; the saver stores every channel value in every checkpoint row."""

    name: ClassVar[str] = "sqlite"
    facts: ClassVar[CheckpointProviderFacts] = CheckpointProviderFacts(
        durable=False, retention="sweep", channel_storage="per_checkpoint"
    )

    async def open(self, conn_string: str | None) -> tuple[CheckpointResource, Callable[[], Awaitable[None]]]:
        """Connect to the file ``conn_string`` and set up the saver's and the kit's tables."""
        if conn_string is None:
            raise ValueError("sqlite checkpoint provider requires a conn_string")

        import aiosqlite  # pyright: ignore[reportMissingImports]
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # pyright: ignore[reportMissingImports]

        from tai42_kit.llm.checkpoint.checkpoint import CheckpointResource

        conn = await aiosqlite.connect(conn_string)
        ledger = SqliteFinishedThreadLedger(conn)
        values = SqliteCheckpointValueStore(conn)
        try:
            await AsyncSqliteSaver(conn).setup()
            await ledger.setup()
            await values.setup()
        except BaseException:
            # No cleanup function is returned on this path: close the connection just opened.
            await conn.close()
            raise

        async def close() -> None:
            await conn.close()

        return CheckpointResource(self.name, conn, ledger, values), close

    def saver(self, resource: CheckpointResource) -> BaseCheckpointSaver:
        """An ``AsyncSqliteSaver`` on the shared connection."""
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver  # pyright: ignore[reportMissingImports]

        return AsyncSqliteSaver(resource.handle)

    def value_encoding(self, saver: BaseCheckpointSaver, compress_threshold: int | None) -> MsgpackValueEncoding:
        """The saver's msgpack, never compressed."""
        from tai42_kit.llm.checkpoint.value_encoding import MsgpackValueEncoding

        return MsgpackValueEncoding(saver.serde, None)

    async def recorded_generation(self, resource: CheckpointResource) -> int | None:
        """The generation the marker table records."""
        await resource.handle.execute(_FORMAT_SETUP)
        await resource.handle.commit()
        async with resource.handle.execute(f"SELECT generation FROM {FORMAT_TABLE} WHERE id = 1") as cursor:  # noqa: S608 -- constant table name
            row = await cursor.fetchone()
        return None if row is None else int(row[0])

    async def record_generation(self, resource: CheckpointResource, generation: int) -> None:
        """Record ``generation`` unless one is recorded."""
        await resource.handle.execute(
            f"INSERT INTO {FORMAT_TABLE} (id, generation) VALUES (1, ?) ON CONFLICT (id) DO NOTHING",  # noqa: S608 -- constant table name
            (generation,),
        )
        await resource.handle.commit()

    async def holds_checkpoints(self, resource: CheckpointResource) -> bool:
        """Whether the saver's ``checkpoints`` table holds a row."""
        async with resource.handle.execute("SELECT 1 FROM checkpoints LIMIT 1") as cursor:
            return await cursor.fetchone() is not None
