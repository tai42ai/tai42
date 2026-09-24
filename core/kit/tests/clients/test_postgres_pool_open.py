"""The pooled Postgres client awaits its initial fill and tears the pool down on any open failure.

``PostgresClient._create`` opens the pool with ``wait=True`` and a DSN-derived timeout so a
one-shot command finishes the ``min_size`` fill before it returns, and wraps the open in
``except BaseException: close``. A fill that misses ``min_size`` in time (``PoolTimeout``) and a
cancelled open (``CancelledError``, a ``BaseException``) both close the pool and re-raise, so no
half-open pool is ever handed out. ``_open_timeout`` budgets the DSN's own ``connect_timeout``
across the fill, falling back to a fixed default when the DSN sets none or something unparseable.
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


def test_open_timeout_falls_back_on_unparseable_connect_timeout():
    dsn = "postgresql://u@h/db?connect_timeout=abc"
    assert pg_mod._open_timeout(dsn, 3) == pg_mod._DEFAULT_OPEN_TIMEOUT


def test_open_timeout_falls_back_on_malformed_dsn():
    # A bare word is not a valid conninfo; conninfo_to_dict raises and the budget defaults.
    assert pg_mod._open_timeout("not a dsn", 3) == pg_mod._DEFAULT_OPEN_TIMEOUT
