"""The kit connection's connect: one loop over the whole connect.

A plain retry makes the attempts redis-py makes, a retried call nested inside the attempt
makes one try, a connect re-entered from inside an attempt fails that attempt, and a fault
outside the retried errors raises at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("redis")

import redis
import redis.asyncio.connection as async_connection_mod
import redis.connection as sync_connection_mod
from redis.asyncio import ConnectionPool as AsyncConnectionPool
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from tai42_kit.clients.impl import redis as redis_mod
from tai42_kit.clients.impl import redis_connections as connections_mod

from ._redis_kit_connections import async_kit_connection, kit_options, sync_kit_connection
from ._redis_log import LOGGER, kit_warnings
from ._redis_socket_opens import ConnectCounter, count_async_connects, count_sync_connects
from ._redis_stub import closed_loopback_port, serving_stub


def _time_out_sync_socket_opens(monkeypatch: pytest.MonkeyPatch) -> ConnectCounter:
    counter = ConnectCounter()

    def _connect(self: Any) -> Any:
        counter.calls += 1
        raise TimeoutError("timed out")

    monkeypatch.setattr(sync_connection_mod.Connection, "_connect", _connect)
    return counter


def _fail_async_socket_opens(monkeypatch: pytest.MonkeyPatch, error: Exception) -> ConnectCounter:
    counter = ConnectCounter()

    async def _connect(self: Any) -> None:
        counter.calls += 1
        raise error

    monkeypatch.setattr(async_connection_mod.Connection, "_connect", _connect)
    return counter


def test_sync_a_plain_retry_on_a_kit_connection_makes_the_attempts_redis_py_makes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _time_out_sync_socket_opens(monkeypatch)
    url = "redis://127.0.0.1:6379/0"
    with pytest.raises(RedisTimeoutError):
        redis.ConnectionPool.from_url(url, retry_on_timeout=True).make_connection().connect()
    plain_attempts, counter.calls = counter.calls, 0
    with pytest.raises(RedisTimeoutError):
        sync_kit_connection(url, retry_on_timeout=True).connect()
    assert plain_attempts == counter.calls == 2


async def test_async_a_plain_retry_on_a_kit_connection_makes_the_attempts_redis_py_makes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _fail_async_socket_opens(monkeypatch, TimeoutError("timed out"))
    url = "redis://127.0.0.1:6379/0"
    with pytest.raises(RedisTimeoutError):
        await AsyncConnectionPool.from_url(url, retry_on_timeout=True).make_connection().connect()
    plain_attempts, counter.calls = counter.calls, 0
    with pytest.raises(RedisTimeoutError):
        await async_kit_connection(url, retry_on_timeout=True).connect()
    assert plain_attempts == counter.calls == 2


# A command's operation that reconnects runs its connect once per try of the operation:
# the plain retry's one retry bounds the socket opens of the whole operation.
def test_sync_a_plain_retry_on_a_kit_connection_bounds_a_command_that_reconnects_by_its_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _time_out_sync_socket_opens(monkeypatch)
    connection = sync_kit_connection("redis://127.0.0.1:6379/0", retry_on_timeout=True)
    with pytest.raises(RedisTimeoutError):
        connection.retry.call_with_retry(lambda: connection.send_command("PING"), lambda error: connection.disconnect())
    assert counter.calls == 2


async def test_async_a_plain_retry_on_a_kit_connection_bounds_a_command_that_reconnects_by_its_own_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _fail_async_socket_opens(monkeypatch, TimeoutError("timed out"))
    connection = async_kit_connection("redis://127.0.0.1:6379/0", retry_on_timeout=True)
    with pytest.raises(RedisTimeoutError):
        await connection.retry.call_with_retry(
            lambda: connection.send_command("PING"), lambda error: connection.disconnect()
        )
    assert counter.calls == 2


def test_sync_a_refused_socket_outside_the_connections_retry_on_error_raises_at_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    counter = count_sync_connects(monkeypatch)
    connection = sync_kit_connection(f"redis://127.0.0.1:{closed_loopback_port()}/0", retry_on_timeout=True)
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
        connection.connect()
    assert counter.calls == 1
    assert kit_warnings(caplog) == []


async def test_async_a_refused_socket_outside_the_connections_retry_on_error_raises_at_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    counter = count_async_connects(monkeypatch)
    connection = async_kit_connection(f"redis://127.0.0.1:{closed_loopback_port()}/0", retry_on_timeout=True)
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
        await connection.connect()
    assert counter.calls == 1
    assert kit_warnings(caplog) == []


class _OnConnect:
    """A connect callback (redis-py keeps it as a weak method) that runs ``action`` on each connect."""

    def __init__(self, action: Callable[[Any], Any]) -> None:
        self.action = action

    def callback(self, connection: Any) -> Any:
        return self.action(connection)


def _one_try(retry: Any) -> int:
    """How many times ``retry`` runs a ``do`` that always fails with a connection error."""
    calls = 0

    def do() -> None:
        nonlocal calls
        calls += 1
        raise RedisConnectionError("down")

    with contextlib.suppress(RedisConnectionError):
        retry.call_with_retry(do, lambda error: None)
    return calls


async def test_sync_inside_the_attempt_the_connections_retry_makes_one_try() -> None:
    async with serving_stub() as stub:
        connection = sync_kit_connection(stub.url, **kit_options(3, sync=True))
        seen: list[tuple[type, int]] = []
        on_connect = _OnConnect(lambda conn: seen.append((type(conn.retry), _one_try(conn.retry))))
        connection.register_connect_callback(on_connect.callback)
        try:
            await asyncio.to_thread(connection.connect)
            outside = connection.retry
        finally:
            connection.disconnect()
    assert seen == [(connections_mod._SyncConnectionRetry, 1)]
    assert type(outside) is connections_mod._SyncConnectionRetry
    assert type(outside._given) is redis_mod._LoggedSyncRetry
    assert _one_try(outside) == 4


async def test_async_inside_the_attempt_the_connections_retry_makes_one_try() -> None:
    async def one_async_try(retry: Any) -> int:
        calls = 0

        async def do() -> None:
            nonlocal calls
            calls += 1
            raise RedisConnectionError("down")

        async def fail(error: BaseException) -> None:
            pass

        with contextlib.suppress(RedisConnectionError):
            await retry.call_with_retry(do, fail)
        return calls

    async with serving_stub() as stub:
        connection = async_kit_connection(stub.url, **kit_options(3, sync=False))
        seen: list[tuple[type, int]] = []

        async def record(conn: Any) -> None:
            seen.append((type(conn.retry), await one_async_try(conn.retry)))

        on_connect = _OnConnect(record)
        connection.register_connect_callback(on_connect.callback)
        try:
            await connection.connect()
            outside = connection.retry
        finally:
            await connection.disconnect()
    assert seen == [(connections_mod._AsyncConnectionRetry, 1)]
    assert type(outside) is connections_mod._AsyncConnectionRetry
    assert type(outside._given) is redis_mod._LoggedAsyncRetry
    assert await one_async_try(outside) == 4


@pytest.mark.parametrize(("retry_attempts", "warnings"), [(1, 1), (0, 0)])
async def test_sync_a_reconnect_from_inside_the_attempt_fails_that_attempt(
    retry_attempts: int, warnings: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        counter = count_sync_connects(monkeypatch)
        connection = sync_kit_connection(stub.url, **kit_options(retry_attempts, sync=True))
        runs: list[None] = []

        def reconnect_on_the_first_run(conn: Any) -> None:
            runs.append(None)
            if len(runs) == 1:
                conn.disconnect()
                conn.connect()

        on_connect = _OnConnect(reconnect_on_the_first_run)
        connection.register_connect_callback(on_connect.callback)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                if retry_attempts:
                    await asyncio.to_thread(connection.connect)
                    assert connection._sock is not None
                else:
                    with pytest.raises(RedisConnectionError):
                        await asyncio.to_thread(connection.connect)
        finally:
            connection.disconnect()
    assert len(kit_warnings(caplog)) == warnings
    assert counter.calls == 1 + warnings


@pytest.mark.parametrize(("retry_attempts", "warnings"), [(1, 1), (0, 0)])
async def test_async_a_reconnect_from_inside_the_attempt_fails_that_attempt(
    retry_attempts: int, warnings: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        counter = count_async_connects(monkeypatch)
        connection = async_kit_connection(stub.url, **kit_options(retry_attempts, sync=False))
        runs: list[None] = []

        async def reconnect_on_the_first_run(conn: Any) -> None:
            runs.append(None)
            if len(runs) == 1:
                await conn.disconnect()
                await conn.connect()

        on_connect = _OnConnect(reconnect_on_the_first_run)
        connection.register_connect_callback(on_connect.callback)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                if retry_attempts:
                    await connection.connect()
                    assert connection.is_connected
                else:
                    with pytest.raises(RedisConnectionError):
                        await connection.connect()
        finally:
            await connection.disconnect()
    assert len(kit_warnings(caplog)) == warnings
    assert counter.calls == 1 + warnings


async def test_async_a_non_socket_fault_in_the_socket_open_is_typed_a_connection_error_and_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    counter = _fail_async_socket_opens(monkeypatch, ValueError("bad config"))
    connection = async_kit_connection("redis://127.0.0.1:6379/0", **kit_options(2, sync=False))
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as raised:
        await connection.connect()
    assert isinstance(raised.value.__cause__, ValueError)
    assert counter.calls == 3
    warnings = kit_warnings(caplog)
    assert len(warnings) == 2
    assert all("bad config" in warning for warning in warnings)


def test_sync_a_non_socket_fault_in_the_socket_open_is_raised_at_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls: list[None] = []

    def _connect(self: Any) -> Any:
        calls.append(None)
        raise ValueError("bad config")

    monkeypatch.setattr(sync_connection_mod.Connection, "_connect", _connect)
    connection = sync_kit_connection("redis://127.0.0.1:6379/0", **kit_options(2, sync=True))
    with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(ValueError, match="bad config"):
        connection.connect()
    assert len(calls) == 1
    assert kit_warnings(caplog) == []
