"""The pooled-connection + transaction-boundary seam every store method runs on."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from tai42_kit.clients.impl.postgres import PostgresClient
from tai42_kit.db import component_store_settings

from tai42_skeleton.states.db import STATES_COMPONENT

from .base import _StoreBase


def _settings() -> Any:
    """The bound store's runtime connection settings, resolved fresh per call."""
    return component_store_settings(STATES_COMPONENT)


def _pool(settings: Any) -> Any:
    """Open a pooled client through the live ``states.store`` package's ``client_ctx``.

    Read from :data:`sys.modules` at CALL time so a test's package-alias monkeypatch
    (and a config reload's rebound package) bites, never a stale module-top binding.
    """
    return sys.modules["tai42_skeleton.states.store"].client_ctx(PostgresClient, settings)


class _StoreConnection(_StoreBase):
    """The pooled-connection and transaction-boundary methods every mixin's read/write runs on.

    Each method opens its own pooled connection; a caller that must span several writes
    atomically opens :meth:`begin` and threads the yielded connection into the write
    methods' ``conn`` parameter — they join that transaction instead of opening their own.
    """

    @asynccontextmanager
    async def begin(self) -> AsyncIterator[AsyncConnection[Any]]:
        """A pooled connection with an open transaction — the atomic boundary for multi-write callers.

        A caller threads it into the write methods' ``conn`` so several writes commit or
        roll back as one.
        """
        async with (
            _pool(_settings()) as pool,
            pool.connection() as conn,
            conn.transaction(),
        ):
            yield conn

    @asynccontextmanager
    async def _write_cursor(self, conn: AsyncConnection[Any] | None) -> AsyncIterator[Any]:
        """Yield the cursor a write runs on.

        With an external ``conn`` join the caller's transaction (no new one); otherwise
        open a pooled connection and a fresh transaction.
        """
        if conn is not None:
            async with conn.cursor(row_factory=dict_row) as cur:
                yield cur
            return
        async with (
            _pool(_settings()) as pool,
            pool.connection() as own,
            own.transaction(),
            own.cursor(row_factory=dict_row) as cur,
        ):
            yield cur

    @asynccontextmanager
    async def _read_cursor(self, conn: AsyncConnection[Any] | None) -> AsyncIterator[Any]:
        """Yield the cursor a read runs on.

        With an external ``conn`` join the caller's transaction (so it sees that
        transaction's own uncommitted writes); otherwise open a pooled connection (no
        transaction, matching a plain read).
        """
        if conn is not None:
            async with conn.cursor(row_factory=dict_row) as cur:
                yield cur
            return
        async with (
            _pool(_settings()) as pool,
            pool.connection() as own,
            own.cursor(row_factory=dict_row) as cur,
        ):
            yield cur
