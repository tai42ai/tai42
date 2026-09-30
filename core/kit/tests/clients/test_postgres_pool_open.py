"""The pooled Postgres client awaits its initial fill and tears the pool down on any open failure.

``PostgresClient._create`` opens the pool with ``wait=True`` and a DSN-derived timeout so a
one-shot command finishes the ``min_size`` fill before it returns, and wraps the open in
``except BaseException: close``. A fill that misses ``min_size`` in time (``PoolTimeout``) and a
cancelled open (``CancelledError``, a ``BaseException``) both close the pool and re-raise, so no
half-open pool is ever handed out. ``_open_timeout`` budgets the DSN's own ``connect_timeout``
across the fill, falling back to a fixed default when the DSN sets none, something unparseable,
or a non-positive value.
Hermetic: the probe connect and the pool are fakes, no real Postgres.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

from psycopg_pool import PoolTimeout

from tai42_kit.clients.impl import postgres as pg_mod

_DSN = "postgresql://u@h/db"


class _FakeProbe:
    """A pre-flight connection that accepts the ``async with`` and records its close."""

    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        self.closed = True
        return False


def _install_probe(monkeypatch):
    probe = _FakeProbe()

    async def _fake_connect(conninfo):
        return probe

    monkeypatch.setattr(pg_mod.AsyncConnection, "connect", _fake_connect)
    return probe


def _install_pool(monkeypatch, *, open_raises):
    """Install a fake pool whose ``open`` raises ``open_raises``; return the captured instances."""
    pools = []

    class _FakePool:
        @staticmethod
        async def check_connection(conn):
            pass

        def __init__(self, **kwargs):
            self.closed = False
            pools.append(self)

        async def open(self, *, wait=False, timeout=None):
            raise open_raises

        async def close(self):
            self.closed = True

    monkeypatch.setattr(pg_mod, "AsyncConnectionPool", _FakePool)
    return pools


def _install_ok_pool(monkeypatch):
    """Install a fake pool that opens cleanly and records each instance's ctor kwargs."""
    pools = []

    class _FakePool:
        @staticmethod
        async def check_connection(conn):
            pass

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.closed = False
            pools.append(self)

        async def open(self, *, wait=False, timeout=None):
            self.opened = True

        async def close(self):
            self.closed = True

    monkeypatch.setattr(pg_mod, "AsyncConnectionPool", _FakePool)
    return pools


def _make_pg_cls():
    """A fresh PostgresClient subclass per test so the class-keyed pool never leaks."""

    class _PG(pg_mod.PostgresClient):
        pass

    return _PG


def test_pool_name_from_dsn_and_owner():
    # The name carries the owning component and the DSN's host/db, never the user
    # or password.
    assert pg_mod.postgres_pool_name("postgresql://u:secret@h:5433/db", "skeleton") == "skeleton@h/db"
    assert pg_mod.postgres_pool_name("postgresql://u:secret@h:5433/db", "skeleton", "pinned") == "skeleton@h/db:pinned"


def test_pool_owner_strips_database_prefix():
    assert pg_mod._pool_owner("TAI_DATABASE_SKELETON_") == "skeleton"
    assert pg_mod._pool_owner("TAI_DATABASE_") == "postgres"
    assert pg_mod._pool_owner("") == "postgres"
    assert pg_mod._pool_owner(None) == "postgres"


def test_pool_owner_strips_prefix_case_insensitively():
    # The registry uppercases prefixes, but a lower- or mixed-case one must derive the
    # same owner rather than leak the raw prefix into the pool name.
    assert pg_mod._pool_owner("tai_database_skeleton_") == "skeleton"
    assert pg_mod._pool_owner("Tai_Database_Accounts_") == "accounts"


def test_mask_dsn_password_masks_the_password():
    masked = pg_mod._mask_dsn_password("postgresql://u:secret@h:5433/db")
    assert "secret" not in masked
    assert "***" in masked


def test_mask_dsn_password_redacts_an_unparseable_dsn():
    # A DSN the parser cannot read carries a password this helper cannot locate to
    # mask, so it is never echoed back — a fully redacted placeholder is returned.
    masked = pg_mod._mask_dsn_password("garbage s3cr3t not a dsn")
    assert masked == "<unparseable DSN>"
    assert "s3cr3t" not in masked


async def test_one_pool_per_dsn_across_prefixes_with_equal_sizes(monkeypatch):
    from tai42_kit.clients import shutdown_all_clients

    _install_probe(monkeypatch)
    pools = _install_ok_pool(monkeypatch)
    cls = _make_pg_cls()
    dsn = "postgresql://u:secret@h:5433/db"
    a = {"dsn": dsn, "min_size": 2, "max_size": 10, "env_prefix": "TAI_DATABASE_SKELETON_"}
    b = {"dsn": dsn, "min_size": 2, "max_size": 10, "env_prefix": "TAI_DATABASE_ACCOUNTS_"}
    try:
        async with cls().current(**a) as p1, cls().current(**b) as p2:
            assert p1 is p2  # one DSN -> one pool, despite differing prefixes
        assert len(pools) == 1
        # The name is the first builder's owner and the DSN host/db — no credentials.
        assert pools[0].kwargs["name"] == "skeleton@h/db"
        assert "secret" not in pools[0].kwargs["name"]
    finally:
        await shutdown_all_clients()


async def test_caller_omitting_sizes_shares_pool_with_default_sizes(monkeypatch):
    from tai42_kit.clients import shutdown_all_clients

    _install_probe(monkeypatch)
    pools = _install_ok_pool(monkeypatch)
    cls = _make_pg_cls()
    dsn = "postgresql://u:secret@h:5433/db"
    try:
        # One caller omits the sizes (the defaults apply), the other passes those exact
        # defaults. Comparing the EFFECTIVE build (defaults filled in), both resolve to
        # one pool rather than conflicting.
        async with (
            cls().current(dsn=dsn) as p1,
            cls().current(dsn=dsn, min_size=pg_mod._DEFAULT_MIN_SIZE, max_size=pg_mod._DEFAULT_MAX_SIZE) as p2,
        ):
            assert p1 is p2
        assert len(pools) == 1
        # A size that differs from the omitting caller's effective default still conflicts.
        async with cls().current(dsn=dsn):
            with pytest.raises(ValueError, match="build options"):
                async with cls().current(dsn=dsn, min_size=pg_mod._DEFAULT_MIN_SIZE + 3):
                    pass
    finally:
        await shutdown_all_clients()


async def test_differing_sizes_on_one_dsn_raise_with_masked_password(monkeypatch):
    from tai42_kit.clients import shutdown_all_clients

    _install_probe(monkeypatch)
    _install_ok_pool(monkeypatch)
    cls = _make_pg_cls()
    dsn = "postgresql://u:secret@h:5433/db"
    try:
        async with cls().current(dsn=dsn, min_size=2, max_size=10, env_prefix="TAI_DATABASE_SKELETON_"):
            with pytest.raises(ValueError, match="build options") as excinfo:
                async with cls().current(dsn=dsn, min_size=5, max_size=20, env_prefix="TAI_DATABASE_SKELETON_"):
                    pass
        message = str(excinfo.value)
        # The password never appears; the mask does.
        assert "secret" not in message
        assert "***" in message
        # Both the recorded and the requested sizes are named.
        assert "'min_size': 2" in message
        assert "'min_size': 5" in message
    finally:
        await shutdown_all_clients()


async def test_pinned_connection_builds_a_one_connection_pinned_pool(monkeypatch):
    from pydantic import SecretStr

    from tai42_kit.clients.settings import PostgresConnectionSettings

    _install_probe(monkeypatch)
    pools = _install_ok_pool(monkeypatch)

    class _FakeConnCtx:
        async def __aenter__(self):
            return "conn"

        async def __aexit__(self, *exc):
            return False

    # The fake pool the seam builds yields a sentinel connection.
    def _connection(self):
        return _FakeConnCtx()

    monkeypatch.setattr(pg_mod.AsyncConnectionPool, "connection", _connection, raising=False)

    settings = PostgresConnectionSettings(
        pg_host="h", pg_port=5433, pg_db="db", pg_user="u", pg_password=SecretStr("secret")
    )
    async with pg_mod.pinned_connection(settings) as conn:
        assert conn == "conn"

    assert len(pools) == 1
    pool = pools[0]
    # A dedicated single connection, named with the :pinned suffix, closed on exit.
    assert pool.kwargs["min_size"] == 1
    assert pool.kwargs["max_size"] == 1
    assert pool.kwargs["name"] == "postgres@h/db:pinned"
    assert pool.closed is True


async def test_fill_timeout_closes_the_pool_and_reraises(monkeypatch):
    _install_probe(monkeypatch)
    timeout = PoolTimeout("min_size not reached in time")
    pools = _install_pool(monkeypatch, open_raises=timeout)

    with pytest.raises(PoolTimeout) as excinfo:
        await pg_mod.PostgresClient()._create(dsn=_DSN, min_size=3)

    # The pool's own error surfaces unchanged, and the half-filled pool is torn down —
    # none is handed back to the caller.
    assert excinfo.value is timeout
    assert len(pools) == 1
    assert pools[0].closed is True


async def test_cancelled_open_closes_the_pool_and_propagates(monkeypatch):
    _install_probe(monkeypatch)
    pools = _install_pool(monkeypatch, open_raises=asyncio.CancelledError())

    # CancelledError is a BaseException; the guard still bounds the pool's lifetime.
    with pytest.raises(asyncio.CancelledError):
        await pg_mod.PostgresClient()._create(dsn=_DSN, min_size=3)

    assert len(pools) == 1
    assert pools[0].closed is True


def test_open_timeout_falls_back_when_dsn_sets_no_connect_timeout():
    assert pg_mod._open_timeout(_DSN, 3) == pg_mod._DEFAULT_OPEN_TIMEOUT


def test_open_timeout_multiplies_connect_timeout_by_min_size():
    dsn = "postgresql://u@h/db?connect_timeout=10"
    # Derived, not the fallback: min_size 1 gives 10.0, which the fixed default is not.
    assert pg_mod._open_timeout(dsn, 3) == 30.0
    assert pg_mod._open_timeout(dsn, 1) == 10.0


def test_open_timeout_treats_min_size_zero_as_one():
    assert pg_mod._open_timeout("postgresql://u@h/db?connect_timeout=7", 0) == 7.0


def test_open_timeout_reads_keyword_value_dsn_form():
    assert pg_mod._open_timeout("host=h connect_timeout=5", 4) == 20.0


def test_open_timeout_falls_back_when_connect_timeout_is_zero():
    # libpq reads 0 as "no timeout"; the fill budget stays finite via the default.
    assert pg_mod._open_timeout("postgresql://u@h/db?connect_timeout=0", 3) == pg_mod._DEFAULT_OPEN_TIMEOUT


def test_open_timeout_falls_back_on_negative_connect_timeout():
    assert pg_mod._open_timeout("postgresql://u@h/db?connect_timeout=-5", 3) == pg_mod._DEFAULT_OPEN_TIMEOUT


def test_open_timeout_falls_back_on_unparseable_connect_timeout():
    dsn = "postgresql://u@h/db?connect_timeout=abc"
    assert pg_mod._open_timeout(dsn, 3) == pg_mod._DEFAULT_OPEN_TIMEOUT


def test_open_timeout_falls_back_on_malformed_dsn():
    # A bare word is not a valid conninfo; conninfo_to_dict raises and the budget defaults.
    assert pg_mod._open_timeout("not a dsn", 3) == pg_mod._DEFAULT_OPEN_TIMEOUT
