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
