"""What every LangGraph Postgres component (checkpoint saver, store) requires of its connections.

The saver and the store need connections that autocommit, skip prepared statements, and yield
dict rows; they ride as a dedicated pool's per-connection kwargs. Their ``setup()`` runs one at
a time per database under :func:`serialised_setup`.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import MappingProxyType
from typing import Any, Final

from psycopg import AsyncConnection
from psycopg.rows import dict_row

LANGGRAPH_CONNECTION_KWARGS: Final[MappingProxyType[str, Any]] = MappingProxyType(
    {"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row}
)

_SETUP_LOCK_POLL_SECONDS: Final = 0.05


@asynccontextmanager
async def serialised_setup(conn: AsyncConnection[Any], name: str) -> AsyncIterator[None]:
    """Hold the session-scoped advisory lock ``hashtext(name)`` on ``conn`` for the body.

    A LangGraph ``setup()`` creates its tables and records its migrations without a lock of its
    own, so concurrent first builds against one database collide on the catalog and on the
    migrations table. Its migrations run ``CREATE INDEX CONCURRENTLY``, which waits for every
    open snapshot in the database: a builder blocked inside ``pg_advisory_lock`` holds one, and
    the two deadlock. So the lock is taken by polling ``pg_try_advisory_lock``, which holds no
    snapshot between attempts. ``conn`` must autocommit, so the session lock spans the body's
    statements. When the unlock fails the connection is closed, which releases the lock, and the
    error propagates: a pooled connection still holding it would block every later build.
    """
    while True:
        cursor = await conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (name,))
        row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(f"pg_try_advisory_lock returned no row for the {name!r} setup lock")
        if _locked(row):
            break
        await asyncio.sleep(_SETUP_LOCK_POLL_SECONDS)
    try:
        yield
    finally:
        try:
            await conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,))
        except BaseException:
            await conn.close()
            raise


def _locked(row: object) -> bool:
    if isinstance(row, dict):
        return bool(row["locked"])
    return bool(row[0])  # type: ignore[index]
