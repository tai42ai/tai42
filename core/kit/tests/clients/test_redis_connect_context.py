"""The kit's async Redis clients connect from an empty context, never their caller's.

A transport keeps the context it was opened in for the connection's life, and a pooled
connection outlives the caller whose command opened it. Each case opens a connection from
a caller whose context holds a marker object and checks the marker is collected once the
caller is gone while the connection stays pooled. The server is an in-process stub that
answers every command the client sends while it connects and pings.
"""

from __future__ import annotations

import asyncio
import contextvars
import gc
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

pytest.importorskip("redis")

from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.connection import (
    AbstractConnection,
    Connection,
    SSLConnection,
    UnixDomainSocketConnection,
)

from tai42_kit.clients import client_ctx
from tai42_kit.clients.impl.redis import RedisClient, async_redis_from_url

_caller: contextvars.ContextVar[object] = contextvars.ContextVar("caller")


class _Marker:
    """An object only the caller's context holds."""


@dataclass
class _Stub:
    """A running stub server: the URL a client dials, how many connections it accepted, and whether one closed."""

    url: str = ""
    connections: int = 0
    closed: asyncio.Event = field(default_factory=asyncio.Event)


def _command_answerer(stub: _Stub, *, close_after_ping: bool = False):
    """A connection handler answering each RESP command: ``+PONG`` to a PING, ``+OK`` to anything else.

    With ``close_after_ping`` it closes the first connection once it has answered a PING.
    """

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stub.connections += 1
        first = stub.connections == 1
        try:
            while header := await reader.readline():
                words = []
                for _ in range(int(header[1:])):
                    length = int((await reader.readline())[1:])
                    words.append((await reader.readexactly(length + 2))[:-2].upper())
                writer.write(b"+PONG\r\n" if words[0] == b"PING" else b"+OK\r\n")
                await writer.drain()
                if close_after_ping and first and words[0] == b"PING":
                    return
        finally:
            writer.close()
            if first:
                stub.closed.set()

    return answer


@asynccontextmanager
async def _tcp_stub(*, close_after_ping: bool = False) -> AsyncIterator[_Stub]:
    stub = _Stub()
    server = await asyncio.start_server(_command_answerer(stub, close_after_ping=close_after_ping), "127.0.0.1", 0)
    async with server:
        stub.url = f"redis://127.0.0.1:{server.sockets[0].getsockname()[1]}/0"
        yield stub


@asynccontextmanager
async def _unix_stub(tmp_path: Path) -> AsyncIterator[_Stub]:
    stub = _Stub()
    path = tmp_path / "redis.sock"
    server = await asyncio.start_unix_server(_command_answerer(stub), path=str(path))
    async with server:
        stub.url = f"unix://{path}"
        yield stub


async def _caller_marker_after(command: Callable[[], Awaitable[object]]) -> weakref.ref[_Marker]:
    """Run ``command`` in a caller task whose context holds a marker; return a weak reference to the marker."""

    async def caller() -> weakref.ref[_Marker]:
        marker = _Marker()
        _caller.set(marker)
        await command()
        return weakref.ref(marker)

    marker = await asyncio.create_task(caller())
    # The finished caller task is dropped once the loop has run the callbacks it scheduled.
    await asyncio.sleep(0)
    gc.collect()
    return marker


async def test_a_pooled_client_connection_keeps_no_context_of_the_caller_that_opened_it() -> None:
    async with _tcp_stub() as stub:
        url = stub.url
        client = RedisClient()

        async def ping() -> None:
            async with client_ctx(RedisClient, url=url) as redis:
                assert await redis.ping() is True

        try:
            marker = await _caller_marker_after(ping)
            async with client.current(url=url) as redis:
                assert len(redis.connection_pool._available_connections) == 1
            assert marker() is None
        finally:
            await client.close(url=url)


async def test_a_unix_socket_connection_keeps_no_context_of_its_caller(tmp_path: Path) -> None:
    async with _unix_stub(tmp_path) as stub:
        redis = async_redis_from_url(stub.url)
        try:
            marker = await _caller_marker_after(redis.ping)
            assert marker() is None
        finally:
            await redis.aclose()


async def test_a_reconnect_of_a_pooled_connection_keeps_no_context_of_its_caller() -> None:
    async with _tcp_stub(close_after_ping=True) as stub:
        redis = async_redis_from_url(stub.url)
        try:
            await redis.ping()
            await asyncio.wait_for(stub.closed.wait(), 5)
            # The client sees the close once the end of the stream reaches its socket.
            await asyncio.sleep(0.05)
            # The pooled connection was closed by the server; the next command reconnects it.
            marker = await _caller_marker_after(redis.ping)
            assert stub.connections == 2
            assert marker() is None
        finally:
            await redis.aclose()


@pytest.mark.parametrize(
    ("url", "url_class"),
    [
        ("redis://127.0.0.1:6379/0", Connection),
        ("rediss://127.0.0.1:6380/0", SSLConnection),
        ("unix:///run/redis.sock", UnixDomainSocketConnection),
    ],
    ids=["tcp", "tls", "unix"],
)
def test_every_url_scheme_selects_its_connection_with_an_empty_context_connect(
    url: str, url_class: type[AbstractConnection]
) -> None:
    connection_class = async_redis_from_url(url).connection_pool.connection_class
    assert issubclass(connection_class, url_class)
    assert connection_class.connect is not url_class.connect


def test_the_client_is_the_from_url_client_over_the_empty_context_pool() -> None:
    url = "redis://127.0.0.1:6379/3?socket_timeout=2"
    built = async_redis_from_url(url, decode_responses=True, max_connections=7)
    plain = AsyncRedis.from_url(url, decode_responses=True, max_connections=7)
    assert built.connection_pool.connection_kwargs == plain.connection_pool.connection_kwargs
    assert built.connection_pool.max_connections == plain.connection_pool.max_connections
    assert built.auto_close_connection_pool is plain.auto_close_connection_pool is True


def test_a_connection_class_without_an_empty_context_connect_is_refused() -> None:
    from tai42_kit.clients.impl import redis as redis_impl

    class _OtherConnection(Connection):
        pass

    with pytest.raises(TypeError, match="no empty-context connect is defined for the Redis connection class"):
        redis_impl._EmptyContextConnectionPool(connection_class=_OtherConnection)


async def test_a_caller_cancelled_while_connecting_cancels_the_connect() -> None:
    accepted = asyncio.Event()
    handlers: set[asyncio.Task[object]] = set()

    async def silent(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handlers.add(asyncio.current_task())  # type: ignore[arg-type]
        accepted.set()
        await reader.read()
        writer.close()

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    async with server:
        redis = async_redis_from_url(f"redis://127.0.0.1:{server.sockets[0].getsockname()[1]}/0")
        try:

            async def ping() -> None:
                await redis.ping()

            before = asyncio.all_tasks()
            caller = asyncio.create_task(ping())
            await asyncio.wait_for(accepted.wait(), 5)
            started = asyncio.all_tasks() - before - handlers - {caller}
            assert len(started) == 1
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            (connect,) = started
            await asyncio.sleep(0)
            assert connect.cancelled()
        finally:
            await redis.aclose()
