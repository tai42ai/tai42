"""An in-process stand-in for PostgreSQL transaction-scoped advisory locks.

One :class:`asyncio.Lock` per key, acquired by the ``pg_advisory_xact_lock`` statement
and released when the holder's connection context exits — the scope the real
transaction-scoped lock has. ``contended`` is set the moment an acquire has to WAIT,
which is how a test observes that a second caller is serialized behind the first rather
than running through the same window.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from tai42_kit.clients.impl.postgres import PostgresClient


class FakeAdvisoryLocks:
    def __init__(self) -> None:
        self._locks: dict[tuple[int, int], asyncio.Lock] = {}
        self.contended = asyncio.Event()

    @asynccontextmanager
    async def client_ctx(self, client_cls, settings=None, **kwargs):
        if client_cls is not PostgresClient:
            raise AssertionError(f"unexpected client_cls in fake: {client_cls!r}")
        yield _FakeLockPool(self)

    async def acquire(self, key: tuple[int, int]) -> asyncio.Lock:
        lock = self._locks.setdefault(key, asyncio.Lock())
        if lock.locked():
            self.contended.set()
        await lock.acquire()
        return lock


class _FakeLockPool:
    def __init__(self, locks: FakeAdvisoryLocks) -> None:
        self._locks = locks

    @asynccontextmanager
    async def connection(self):
        conn = _FakeLockConn(self._locks)
        try:
            yield conn
        finally:
            conn.release_all()


class _FakeLockConn:
    def __init__(self, locks: FakeAdvisoryLocks) -> None:
        self._locks = locks
        self._held: list[asyncio.Lock] = []

    @asynccontextmanager
    async def transaction(self):
        yield None

    async def execute(self, sql: str, params: tuple = ()) -> None:
        if "pg_advisory_xact_lock" not in sql:
            raise AssertionError(f"unexpected SQL on the advisory-lock connection: {sql!r}")
        self._held.append(await self._locks.acquire(tuple(params)))

    def release_all(self) -> None:
        while self._held:
            self._held.pop().release()
