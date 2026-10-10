"""Shared helpers and fixtures for the LLM test suites.

The checkpoint saver and the LLM store are built the same way — the kit resolves
a URL, builds the redis client itself and injects it — so both suites assert
against the same two things: where an injected client points, and how many times
it is closed. The ``noop_monitoring`` fixture binds an app whose monitoring writer
is a fail-safe no-op, so a test that invokes a classifier (which records every call
as a model call) has the monitoring facet available without asserting on it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_contract.app import tai42_app


class _NoopSpan:
    @property
    def id(self) -> str:
        return "noop-span"

    def update(self, **_: Any) -> None:
        pass

    def set_trace_metadata(self, **_: Any) -> None:
        pass


class _NoopMonitoringWriter:
    """A monitoring writer whose ``start_span`` yields a no-op span handle."""

    @contextmanager
    def start_span(self, **_: Any) -> Iterator[_NoopSpan]:
        yield _NoopSpan()


@pytest.fixture
def noop_monitoring() -> Iterator[None]:
    """Bind an app exposing a no-op monitoring writer for the test's duration."""
    app = SimpleNamespace(monitoring=SimpleNamespace(active=SimpleNamespace(writer=_NoopMonitoringWriter())))
    with tai42_app.bound(app):
        yield


def client_target(client: Any) -> str:
    """The ``host:port/db`` an (unconnected) redis-py client is pointed at.

    redis-py records only the components the URL named, so the redis defaults
    fill in the rest — exactly as the client itself would resolve them.
    """
    kwargs = client.connection_pool.connection_kwargs
    return f"{kwargs.get('host', 'localhost')}:{kwargs.get('port', 6379)}/{kwargs.get('db', 0)}"


class SpyClient:
    """Stands in for the redis client the kit builds, counting its closes."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.closes = 0

    async def aclose(self) -> None:
        self.closes += 1


def install_spy_client(monkeypatch) -> list[SpyClient]:
    """Make the kit's async Redis client builder hand back close-counting spies."""
    from tai42_kit.clients.impl import redis as redis_impl

    built: list[SpyClient] = []

    def _from_url(url: str, **kwargs: Any) -> SpyClient:
        client = SpyClient(url)
        built.append(client)
        return client

    monkeypatch.setattr(redis_impl, "async_redis_from_url", _from_url)
    return built


def install_fake_named_pool(monkeypatch) -> tuple[dict[str, Any], list[bool]]:
    """Route ``_open_named_pool`` through fakes so a postgres store/checkpoint test can
    assert the pool build (conninfo, sizes, name, per-connection kwargs) and its awaited
    fill without a live server.

    The store/checkpoint factories now build their pool through
    ``tai42_kit.clients.impl.postgres._open_named_pool``, which probes the first
    connection with the real driver before it builds the pool. Faking the driver's
    ``connect`` and ``AsyncConnectionPool`` on that module exercises the shared seam
    (probe, ``wait=True`` fill, checkout-time check, close-on-failure) end to end.

    Returns the pool's captured build kwargs (including ``open_wait``) and a close log.
    """
    from tai42_kit.clients.impl import postgres as pg

    captured: dict[str, Any] = {}
    closed: list[bool] = []

    class _FakeProbe:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _fake_connect(conninfo):
        captured["probe_dsn"] = conninfo
        return _FakeProbe()

    class _FakeConnCtx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    class _FakePool:
        @staticmethod
        async def check_connection(conn):
            pass

        def __init__(self, **kwargs):
            captured.update(kwargs)

        async def open(self, *, wait=False, timeout=None):
            captured["open_wait"] = wait

        def connection(self):
            return _FakeConnCtx()

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(pg.AsyncConnection, "connect", _fake_connect)
    monkeypatch.setattr(pg, "AsyncConnectionPool", _FakePool)
    return captured, closed
