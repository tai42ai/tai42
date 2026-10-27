"""The connection classes and pools a kit Redis client gets.

Every URL scheme gives a sync client the kit connection class over the scheme's own
class, one kit class per base, and the kit async connection class over a base is one
class per base too; the sync and async from-url clients are redis-py's own
from-url clients on the kit's pool and connection.
"""

from __future__ import annotations

import asyncio
import gc
import weakref

import pytest

pytest.importorskip("redis")

import redis
from redis.asyncio import Connection as AsyncConnection
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.connection import AbstractConnection as AsyncAbstractConnection
from redis.asyncio.connection import SSLConnection as AsyncSSLConnection
from redis.asyncio.connection import UnixDomainSocketConnection as AsyncUnixDomainSocketConnection

from tai42_kit.clients.impl import redis as redis_mod
from tai42_kit.clients.impl import redis_connections as connections_mod

from ._redis_pool_kwargs import comparable_connection_kwargs
from ._redis_stub import serving_stub

_SCHEMES = pytest.mark.parametrize(
    ("url", "url_class"),
    [
        ("redis://127.0.0.1:6379/0", redis.Connection),
        ("rediss://127.0.0.1:6380/0", redis.SSLConnection),
        ("unix:///run/redis.sock", redis.UnixDomainSocketConnection),
    ],
    ids=["tcp", "tls", "unix"],
)


@_SCHEMES
def test_every_url_scheme_gives_a_sync_client_the_kit_connection_over_its_class(url: str, url_class: type) -> None:
    pool = redis_mod.KitSyncRedis.from_url(url).connection_pool
    assert type(pool) is connections_mod.KitSyncConnectionPool
    assert issubclass(pool.connection_class, url_class)
    assert pool.connection_class is connections_mod.kit_sync_connection_class(url_class)
    assert pool.connection_class.connect is not url_class.connect


@_SCHEMES
def test_the_sync_from_url_kwargs_carry_the_kit_connection_class_of_the_urls_scheme(url: str, url_class: type) -> None:
    assert redis_mod.redis_from_url_kwargs({"url": url}, sync=True)["connection_class"] is (
        connections_mod.kit_sync_connection_class(url_class)
    )
    assert "connection_class" not in redis_mod.redis_from_url_kwargs({"url": url})


def test_the_kit_sync_connection_class_is_one_class_per_base_and_wraps_once() -> None:
    tcp = connections_mod.kit_sync_connection_class(redis.Connection)
    assert connections_mod.kit_sync_connection_class(redis.Connection) is tcp
    assert connections_mod.kit_sync_connection_class(tcp) is tcp

    # A connection class a pool's owner defines per pool, as a library building its own pools does.
    class PerPool(redis.Connection):
        pass

    wrapped = connections_mod.kit_sync_connection_class(PerPool)
    assert issubclass(wrapped, PerPool)
    assert connections_mod.kit_sync_connection_class(PerPool) is wrapped
    assert connections_mod.kit_sync_connection_class(wrapped) is wrapped

    # Nothing pins a per-pool class once its pool is gone.
    released, base_released = weakref.ref(wrapped), weakref.ref(PerPool)
    del wrapped, PerPool
    gc.collect()
    assert released() is None
    # The collection that frees the kit class drops its entry, which frees the base on the next one.
    gc.collect()
    assert base_released() is None


@pytest.mark.parametrize(
    ("url", "url_class"),
    [
        ("redis://127.0.0.1:6379/0", AsyncConnection),
        ("rediss://127.0.0.1:6380/0", AsyncSSLConnection),
        ("unix:///run/redis.sock", AsyncUnixDomainSocketConnection),
    ],
    ids=["tcp", "tls", "unix"],
)
def test_the_kit_async_connection_class_over_a_url_schemes_class_is_the_async_pools(
    url: str, url_class: type[AsyncAbstractConnection]
) -> None:
    assert AsyncRedis.from_url(url).connection_pool.connection_class is url_class
    pool = redis_mod.async_redis_from_url(url).connection_pool
    assert connections_mod.kit_async_connection_class(url_class) is pool.connection_class


def test_the_kit_async_connection_class_is_one_class_per_base_and_wraps_once() -> None:
    tcp = connections_mod.kit_async_connection_class(AsyncConnection)
    assert connections_mod.kit_async_connection_class(AsyncConnection) is tcp
    assert connections_mod.kit_async_connection_class(tcp) is tcp

    # A connection class a pool's owner defines per pool, as a library building its own pools does.
    class PerPool(AsyncConnection):
        pass

    wrapped = connections_mod.kit_async_connection_class(PerPool)
    assert issubclass(wrapped, PerPool)
    assert issubclass(wrapped, connections_mod._AsyncConnectsUnderTheRetry)
    assert issubclass(wrapped, connections_mod._ConnectsFromAnEmptyContext)
    assert connections_mod.kit_async_connection_class(PerPool) is wrapped
    assert connections_mod.kit_async_connection_class(wrapped) is wrapped

    # Nothing pins a per-pool class once its pool is gone.
    released, base_released = weakref.ref(wrapped), weakref.ref(PerPool)
    del wrapped, PerPool
    gc.collect()
    assert released() is None
    # The collection that frees the kit class drops its entry, which frees the base on the next one.
    gc.collect()
    assert base_released() is None


async def test_a_connection_of_the_kit_async_class_over_a_base_runs_an_action_unretried() -> None:
    class PerPool(AsyncConnection):
        pass

    connection = connections_mod.kit_async_connection_class(PerPool)()
    ran: list[str] = []
    connections_mod.run_unretried(connection, lambda: ran.append("ran"))
    assert ran == ["ran"]


def test_the_sync_client_is_the_from_url_client_over_the_kit_sync_pool() -> None:
    url = "redis://127.0.0.1:6379/3?socket_timeout=2"
    built = redis_mod.KitSyncRedis.from_url(url, decode_responses=True, max_connections=7)
    plain = redis.Redis.from_url(url, decode_responses=True, max_connections=7)
    assert comparable_connection_kwargs(built.connection_pool.connection_kwargs) == comparable_connection_kwargs(
        plain.connection_pool.connection_kwargs
    )
    assert built.connection_pool.max_connections == plain.connection_pool.max_connections
    assert built.auto_close_connection_pool is plain.auto_close_connection_pool is True


async def test_a_sync_single_connection_client_from_url_is_the_from_url_one_on_a_kit_connection() -> None:
    async with serving_stub() as stub:
        built = await asyncio.to_thread(redis_mod.KitSyncRedis.from_url, stub.url, single_connection_client=True)
        plain = await asyncio.to_thread(redis.Redis.from_url, stub.url, single_connection_client=True)
        try:
            assert comparable_connection_kwargs(
                built.connection_pool.connection_kwargs
            ) == comparable_connection_kwargs(plain.connection_pool.connection_kwargs)
            assert built._single_connection_client is plain._single_connection_client is True
            assert built.auto_close_connection_pool is plain.auto_close_connection_pool is True
            assert isinstance(built.connection, connections_mod.kit_sync_connection_class(redis.Connection))
            assert await asyncio.to_thread(built.ping) is True
        finally:
            built.close()
            plain.close()


async def test_an_async_single_connection_client_from_url_is_the_from_url_one_on_a_kit_connection() -> None:
    async with serving_stub() as stub:
        built = await redis_mod.async_redis_from_url(stub.url, single_connection_client=True).initialize()
        plain = await AsyncRedis.from_url(stub.url, single_connection_client=True).initialize()
        try:
            assert comparable_connection_kwargs(
                built.connection_pool.connection_kwargs
            ) == comparable_connection_kwargs(plain.connection_pool.connection_kwargs)
            assert built.single_connection_client is plain.single_connection_client is True
            assert built.auto_close_connection_pool is plain.auto_close_connection_pool is True
            assert isinstance(built.connection, connections_mod._TcpConnection)
            assert await built.ping() is True
        finally:
            await built.aclose()
            await plain.aclose()


def test_the_async_client_from_url_refuses_auto_close_connection_pool_at_the_call() -> None:
    with pytest.raises(TypeError, match="auto_close_connection_pool"):
        redis_mod.async_redis_from_url("redis://127.0.0.1:6379/0", auto_close_connection_pool=True)
