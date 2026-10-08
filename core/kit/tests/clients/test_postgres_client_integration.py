"""A REAL Postgres exercise of the pooled ``PostgresClient`` lifecycle: open →
lease → query → close-under-lease → reopen, plus the checkout-time reconnect that
lets a pooled caller ride out a connection the server dropped (killed here with
``pg_terminate_backend`` from an independent connection).

The pooled client's ``check=AsyncConnectionPool.check_connection`` is what makes
the reconnect real — a dead connection is discarded and replaced on checkout
instead of handed to the caller. There is no fake here. It is OPT-IN: set
``TAI42_KIT_REAL_PG=1`` and point the ``TAI_DATABASE_DEFAULT_PG_*`` env at a live
Postgres. Without the opt-in the test SKIPS VISIBLY with a clear reason (never a
silent skip)."""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

pytest.importorskip("psycopg_pool")

import psycopg
from psycopg import pq, sql

from tai42_kit.clients import advance_client_epoch, client_ctx, drain_epoch
from tai42_kit.clients.impl import postgres as pg
from tai42_kit.clients.impl.postgres import Json, PostgresClient, pinned_connection, read_connection
from tai42_kit.clients.impl.postgres_json import configure_platform_json, platform_json_loads
from tai42_kit.clients.settings import PostgresConnectionSettings
from tai42_kit.db import database_settings

pytestmark = pytest.mark.integration

_OPT_IN_ENV = "TAI42_KIT_REAL_PG"


@pytest.fixture
def real_pg_settings() -> PostgresConnectionSettings:
    if os.environ.get(_OPT_IN_ENV) not in ("1", "true", "True"):
        pytest.skip(
            f"real-Postgres kit client test is opt-in: set {_OPT_IN_ENV}=1 and point the "
            "TAI_DATABASE_DEFAULT_PG_* env at a live Postgres to run it (needs real pool "
            "checkout/reconnect semantics — no fake)"
        )
    # The connection identity comes from the kit's own settings under the DEFAULT
    # database prefix (TAI_DATABASE_DEFAULT_PG_*).
    return database_settings("default")


async def _scalar(pool, sql, params=()):
    async with pool.connection() as conn:
        cur = await conn.execute(sql, params)
        row = await cur.fetchone()
        assert row is not None
        return row[0]


async def test_open_lease_query_close_under_lease_reopen(
    real_pg_settings: PostgresConnectionSettings,
) -> None:
    inst = PostgresClient()
    kwargs = real_pg_settings.client_kwargs()

    async with inst.current(**kwargs) as pool:
        assert await _scalar(pool, "SELECT 1") == 1
        # Retire the epoch while this lease is held: the pooled client stays open
        # and usable until the lease releases, never closed out from under it.
        retired = advance_client_epoch()
        assert await _scalar(pool, "SELECT 2") == 2
    # Lease released -> the real _close (pool.close()) fires; the drain has nothing
    # left to force-close.
    await drain_epoch(retired, 5.0)

    # Reopen: a fresh lease builds a brand-new pool for the same connection key.
    async with inst.current(**kwargs) as pool2:
        assert pool2 is not pool
        assert await _scalar(pool2, "SELECT 1") == 1
    retired2 = advance_client_epoch()
    await drain_epoch(retired2, 5.0)


async def test_pool_reconnects_after_backend_terminated(
    real_pg_settings: PostgresConnectionSettings,
) -> None:
    # A single-connection pool so the killed backend is the exact one the next
    # checkout must reconnect.
    settings = real_pg_settings.model_copy(update={"pg_min_connections": 1, "pg_max_connections": 1})
    inst = PostgresClient()
    kwargs = settings.client_kwargs()

    async with inst.current(**kwargs) as pool:
        pid = await _scalar(pool, "SELECT pg_backend_pid()")

        # Kill that backend from an INDEPENDENT connection (a fresh one-shot pool
        # outside the pool under test).
        async with (
            client_ctx(PostgresClient, settings, fresh=True) as term_pool,
            term_pool.connection() as tconn,
        ):
            await tconn.execute("SELECT pg_terminate_backend(%s)", (pid,))

        # The pooled connection is now dead server-side. On checkout the pool's
        # check discards it and hands back a freshly opened one — a new backend
        # pid, and a query that works.
        new_pid = await _scalar(pool, "SELECT pg_backend_pid()")
        assert await _scalar(pool, "SELECT 1") == 1
        assert new_pid != pid

    retired = advance_client_epoch()
    await drain_epoch(retired, 5.0)


def _jsonb_loads(conn: Any) -> Any:
    """The loads function the connection's binary and text jsonb loaders call."""
    oid = conn.adapters.types["jsonb"].oid
    binary = conn.adapters.get_loader(oid, pq.Format.BINARY)
    text = conn.adapters.get_loader(oid, pq.Format.TEXT)
    assert binary._loads is text._loads
    return binary._loads


async def test_jsonb_reads_through_a_client_pool_use_the_platform_loader(
    real_pg_settings: PostgresConnectionSettings,
) -> None:
    inst = PostgresClient()
    async with inst.current(**real_pg_settings.client_kwargs()) as pool, pool.connection() as conn:
        assert _jsonb_loads(conn) is platform_json_loads
        cur = await conn.execute(
            "SELECT %s::jsonb, %s::json", (Json({"big": 2**64, "neg": -(2**63) - 1}), Json([0.5, None]))
        )
        row = await cur.fetchone()
        assert row is not None
        assert row[0] == {"big": 2**64, "neg": -(2**63) - 1}
        assert type(row[0]["big"]) is int
        assert row[1] == [0.5, None]
    await drain_epoch(advance_client_epoch(), 5.0)


async def test_a_pinned_connection_uses_the_platform_loader(real_pg_settings: PostgresConnectionSettings) -> None:
    async with pinned_connection(real_pg_settings) as conn:
        assert _jsonb_loads(conn) is platform_json_loads


async def test_the_checkpoint_pool_keeps_the_library_loader(real_pg_settings: PostgresConnectionSettings) -> None:
    pytest.importorskip("langgraph.checkpoint.postgres.aio")
    from tai42_kit.llm.checkpoint.checkpoint import create_checkpoint_resource

    resource, closer = await create_checkpoint_resource("postgres", real_pg_settings.pg_dsn)
    try:
        async with resource.handle.connection() as conn:
            assert _jsonb_loads(conn) is json.loads
    finally:
        await closer()


def _one_connection_pool(settings: PostgresConnectionSettings) -> Any:
    """A client pool of exactly one connection, so every checkout is the same connection."""
    one = settings.model_copy(update={"pg_min_connections": 1, "pg_max_connections": 1})
    return PostgresClient().current(**one.client_kwargs())


async def _fetch_one(conn: Any, query: Any, params: tuple = ()) -> Any:
    row = await (await conn.execute(query, params)).fetchone()
    assert row is not None
    return row[0]


async def test_read_connection_runs_each_statement_on_its_own(
    real_pg_settings: PostgresConnectionSettings,
) -> None:
    async with _one_connection_pool(real_pg_settings) as pool:
        async with read_connection(pool) as conn:
            assert conn.autocommit is True
            first = await _fetch_one(conn, "SELECT now()")
            # No BEGIN was sent: the statement ran and the connection is idle.
            assert conn.info.transaction_status == pq.TransactionStatus.IDLE
            await conn.execute("SELECT pg_sleep(0.01)")
            second = await _fetch_one(conn, "SELECT now()")
        assert first != second

        async with pool.connection() as conn:
            assert conn.autocommit is False
            first = await _fetch_one(conn, "SELECT now()")
            assert conn.info.transaction_status == pq.TransactionStatus.INTRANS
            await conn.execute("SELECT pg_sleep(0.01)")
            second = await _fetch_one(conn, "SELECT now()")
        assert first == second
    await drain_epoch(advance_client_epoch(), 5.0)


class _AbortError(RuntimeError):
    pass


async def _insert_then_abort(pool: Any, table: sql.Identifier, expected_pid: int) -> None:
    async with pool.connection() as conn:
        # The same (one) pooled connection, back in transactional mode.
        assert await _fetch_one(conn, "SELECT pg_backend_pid()") == expected_pid
        assert conn.autocommit is False
        await conn.execute(sql.SQL("INSERT INTO {} VALUES (1)").format(table))
        raise _AbortError


async def test_a_connection_returns_to_the_pool_transactional(real_pg_settings: PostgresConnectionSettings) -> None:
    table = sql.Identifier(f"read_conn_{uuid.uuid4().hex[:12]}")
    async with _one_connection_pool(real_pg_settings) as pool:
        async with pool.connection() as conn:
            await conn.execute(sql.SQL("CREATE TABLE {} (v int)").format(table))
        try:
            async with read_connection(pool) as conn:
                pid = await _fetch_one(conn, "SELECT pg_backend_pid()")

            with pytest.raises(_AbortError):
                await _insert_then_abort(pool, table, pid)

            async with pool.connection() as conn:
                assert await _fetch_one(conn, sql.SQL("SELECT count(*) FROM {}").format(table)) == 0
        finally:
            async with pool.connection() as conn:
                await conn.execute(sql.SQL("DROP TABLE {}").format(table))
    await drain_epoch(advance_client_epoch(), 5.0)


async def test_client_pools_restore_transactional_mode_and_pinned_pools_do_not(
    real_pg_settings: PostgresConnectionSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[dict[str, Any]] = []

    class _RecordingPool(pg.AsyncConnectionPool):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            built.append(kwargs)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(pg, "AsyncConnectionPool", _RecordingPool)

    async with _one_connection_pool(real_pg_settings) as pool, pool.connection():
        pass
    await drain_epoch(advance_client_epoch(), 5.0)
    async with pinned_connection(real_pg_settings):
        pass

    client_build, pinned_build = built
    assert client_build["reset"] is pg._restore_transactional
    assert client_build["configure"] is configure_platform_json
    assert pinned_build["reset"] is None
    assert pinned_build["configure"] is configure_platform_json


async def _read_after_own_backend_killed(pool: Any, settings: PostgresConnectionSettings) -> int:
    """Kill the backend of the connection ``read_connection`` holds, then read on it."""
    async with read_connection(pool) as conn:
        pid = await _fetch_one(conn, "SELECT pg_backend_pid()")
        async with (
            client_ctx(PostgresClient, settings, fresh=True) as term_pool,
            term_pool.connection() as tconn,
        ):
            await tconn.execute("SELECT pg_terminate_backend(%s)", (pid,))
        try:
            await conn.execute("SELECT 1")
        except psycopg.OperationalError:
            return pid
    raise AssertionError("a read on a terminated backend did not fail")


async def test_a_connection_broken_inside_read_connection_is_replaced(
    real_pg_settings: PostgresConnectionSettings,
) -> None:
    async with _one_connection_pool(real_pg_settings) as pool:
        pid = await _read_after_own_backend_killed(pool, real_pg_settings)

        async with read_connection(pool) as conn:
            assert await _fetch_one(conn, "SELECT pg_backend_pid()") != pid
    await drain_epoch(advance_client_epoch(), 5.0)
