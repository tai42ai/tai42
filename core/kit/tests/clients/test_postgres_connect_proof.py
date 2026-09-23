"""The pooled Postgres client proves its first connection before it is handed out.

``PostgresClient._create`` opens one bounded ``AsyncConnection.connect`` against the DSN
BEFORE it builds the pool, so a refused / unreachable / non-speaking server surfaces the
real ``psycopg.OperationalError`` at ``client_ctx`` entry — bounded by the DSN's own
``connect_timeout``, not the pool's checkout timeout — with no pool built and no worker
left retrying. Hermetic: only loopback sockets, no real Postgres.
"""

from __future__ import annotations

import contextlib
import socket
import time
from collections.abc import Iterator

import pytest

pytest.importorskip("psycopg")
pytest.importorskip("psycopg_pool")

import psycopg
from psycopg_pool import PoolTimeout
from pydantic import SecretStr

from tai42_kit.clients import PostgresConnectionSettings, client_ctx
from tai42_kit.clients.impl.postgres import PostgresClient


def _closed_loopback_port() -> int:
    """A loopback port that nothing listens on — a connect to it is refused at once."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@contextlib.contextmanager
def _silent_acceptor() -> Iterator[int]:
    """A loopback listener that completes the TCP handshake but never speaks Postgres.

    The kernel accepts the connection into the backlog; the server never sends the startup
    response, so libpq waits out ``connect_timeout`` — the managed-Postgres-over-SSL stall.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def _settings(port: int, *, connect_timeout: int = 10) -> PostgresConnectionSettings:
    return PostgresConnectionSettings(
        pg_host="127.0.0.1",
        pg_port=port,
        pg_password=SecretStr("s3cr3t-probe-pw"),
        pg_min_connections=1,
        pg_max_connections=1,
        pg_connect_timeout=connect_timeout,
    )


class _CountingPostgresClient(PostgresClient):
    """Counts ``_create`` calls so a re-attempt proves no cached pool entry was left behind."""

    creates = 0

    async def _create(self, **kwargs):  # type: ignore[no-untyped-def]
        type(self).creates += 1
        return await super()._create(**kwargs)


async def test_refused_connect_raises_the_real_driver_error_and_leaves_no_pool() -> None:
    settings = _settings(_closed_loopback_port())
    kwargs = settings.client_kwargs()
    inst = _CountingPostgresClient()

    with pytest.raises(psycopg.OperationalError) as first:
        async with inst.current(**kwargs):
            pass
    assert not isinstance(first.value, PoolTimeout)

    # No entry was registered on the failed create, so a following lease re-attempts
    # ``_create`` (rather than resurrecting a half-open pool).
    with pytest.raises(psycopg.OperationalError):
        async with inst.current(**kwargs):
            pass
    assert _CountingPostgresClient.creates == 2


async def test_fresh_path_raises_the_real_driver_error_identically() -> None:
    settings = _settings(_closed_loopback_port())
    with pytest.raises(psycopg.OperationalError) as excinfo:
        async with client_ctx(PostgresClient, settings, fresh=True):
            pass
    assert not isinstance(excinfo.value, PoolTimeout)


async def test_non_speaking_server_times_out_at_connect_timeout_not_the_pool() -> None:
    with _silent_acceptor() as port:
        settings = _settings(port, connect_timeout=2)
        started = time.monotonic()
        with pytest.raises(psycopg.OperationalError) as excinfo:
            async with client_ctx(PostgresClient, settings, fresh=True):
                pass
        elapsed = time.monotonic() - started

    # The libpq connect timeout fired — the real driver error, bounded by the DSN's
    # ``connect_timeout``, well under the pool's 30 s checkout timeout.
    assert not isinstance(excinfo.value, PoolTimeout)
    assert elapsed < 15
