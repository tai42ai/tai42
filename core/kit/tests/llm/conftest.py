"""Shared helpers for the checkpoint/store redis-injection suites.

The checkpoint saver and the LLM store are built the same way — the kit resolves
a URL, builds the redis client itself and injects it — so both suites assert
against the same two things: where an injected client points, and how many times
it is closed.
"""

from typing import Any


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
    """Make ``AsyncRedis.from_url`` hand back close-counting spies."""
    from redis.asyncio import Redis as AsyncRedis

    built: list[SpyClient] = []

    def _from_url(url: str, **kwargs: Any) -> SpyClient:
        client = SpyClient(url)
        built.append(client)
        return client

    monkeypatch.setattr(AsyncRedis, "from_url", staticmethod(_from_url))
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
