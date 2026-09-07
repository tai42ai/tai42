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

import os

import pytest

pytest.importorskip("psycopg_pool")

from tai42_kit.clients import advance_client_epoch, client_ctx, drain_epoch
from tai42_kit.clients.impl.postgres import PostgresClient
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
