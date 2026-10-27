"""Real sockets: a kit connection connects and reconnects under its retry.

The pool's connect retries a refused port, a command whose connection is lost reconnects
under the command's budget, and a pub/sub read or command that meets a dropped socket
reconnects and re-subscribes within its budget, with one WARNING per retried failure. A
command sent on a dropped socket outside any retried operation, as a pub/sub's health
check sends its ``PING``, reconnects under the connection's retry too, its handshake
included. The socket cases dial loopback stubs and closed loopback ports only.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

pytest.importorskip("redis")

from redis.exceptions import ConnectionError as RedisConnectionError

from tai42_kit.clients.impl import redis as redis_mod

from ._redis_kit_connections import async_kit_connection, kit_options, sync_kit_connection
from ._redis_log import LOGGER, kit_warnings, on_warning
from ._redis_socket_opens import count_async_connects, count_sync_connects
from ._redis_stub import HANDSHAKE_DROP, closed_loopback_port, serving_stub


async def test_async_the_pool_connect_retries_a_refused_port_with_warnings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    counter = count_async_connects(monkeypatch)
    url = f"redis://127.0.0.1:{closed_loopback_port()}/0"
    client = await redis_mod.RedisClient()._create(url=url, retry_attempts=2, env_prefix="X_")
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
            await client.ping()
    finally:
        await client.aclose()
    assert counter.calls == 3
    warnings = kit_warnings(caplog)
    assert len(warnings) == 2
    assert warnings[0].startswith("Redis connection X_REDIS_URL failed (attempt 1 of 3): ")


async def test_sync_the_pool_connect_retries_a_refused_port_with_warnings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    counter = count_sync_connects(monkeypatch)
    url = f"redis://127.0.0.1:{closed_loopback_port()}/0"
    client = await redis_mod.SyncRedisClient()._create(url=url, retry_attempts=2)
    try:
        with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
            client.ping()
    finally:
        client.close()
    assert counter.calls == 3
    assert len(kit_warnings(caplog)) == 2


async def test_async_a_connection_lost_mid_command_reconnects_under_the_command_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub(drop_on_ping=2) as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        try:
            assert await client.ping() is True
            counter = count_async_connects(monkeypatch)
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
                await client.ping()
        finally:
            await client.aclose()
    assert counter.calls == 2
    assert len(kit_warnings(caplog)) == 2


async def test_sync_a_connection_lost_mid_command_reconnects_under_the_command_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub(drop_on_ping=2) as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        try:
            assert await asyncio.to_thread(client.ping) is True
            counter = count_sync_connects(monkeypatch)
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
                await asyncio.to_thread(client.ping)
        finally:
            client.close()
    assert counter.calls == 2
    assert len(kit_warnings(caplog)) == 2


async def test_async_a_pubsub_read_rides_a_comeback_within_its_reconnect_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe("events")
            subscribed = await pubsub.get_message(timeout=1)
            assert subscribed is not None
            assert subscribed["type"] == "subscribe"
            counter = count_async_connects(monkeypatch)
            stub.drop()
            loop = asyncio.get_running_loop()
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                async with on_warning(2, lambda: stub.come_back(loop)):
                    message = await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert message is not None
    assert message["type"] == "subscribe"
    assert counter.calls == 3
    assert len(kit_warnings(caplog)) == 3
    assert stub.subscribes == 2


async def test_async_a_pubsub_read_refused_past_its_budget_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            counter = count_async_connects(monkeypatch)
            stub.drop()
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
                await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert counter.calls == 3
    assert len(kit_warnings(caplog)) == 2


async def test_sync_a_pubsub_read_rides_a_comeback_within_its_reconnect_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            subscribed = await asyncio.to_thread(pubsub.get_message, timeout=1)
            assert subscribed is not None
            assert subscribed["type"] == "subscribe"
            counter = count_sync_connects(monkeypatch)
            stub.drop()
            loop = asyncio.get_running_loop()
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                async with on_warning(2, lambda: stub.come_back(loop)):
                    message = await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert message is not None
    assert message["type"] == "subscribe"
    assert counter.calls == 3
    assert len(kit_warnings(caplog)) == 3
    assert stub.subscribes == 2


async def test_sync_a_pubsub_read_refused_past_its_budget_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            counter = count_sync_connects(monkeypatch)
            stub.drop()
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
                await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert counter.calls == 3
    assert len(kit_warnings(caplog)) == 2


async def test_sync_a_pubsub_command_whose_send_fails_reconnects_and_resubscribes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        done = threading.Event()
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            subscribed = await asyncio.to_thread(pubsub.get_message, timeout=1)
            assert subscribed is not None
            assert subscribed["type"] == "subscribe"
            await stub.drop_closed()
            stub.come_back(asyncio.get_running_loop())

            def subscribe_more() -> None:
                # The second send at the latest meets the dropped socket; its retry's
                # reconnect re-subscribes "events" while the failed command is in flight.
                pubsub.subscribe("alerts")
                pubsub.subscribe("status")
                done.set()

            # A daemon thread, so a command that never returns fails the test instead of hanging it.
            worker = threading.Thread(target=subscribe_more, daemon=True)
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                worker.start()
                await asyncio.to_thread(worker.join, 5)
            assert done.is_set(), "the pub/sub command never returned after its reconnect"
            # A send returns before the stub reads it; the stub answers each SUBSCRIBE on
            # one connection in order, so its answer to "status" means it counted them all.
            replies: list[tuple[Any, Any]] = []
            while len(replies) < 3:
                reply = await asyncio.to_thread(pubsub.get_message, timeout=5)
                assert reply is not None, f"the stub stopped answering after {replies}"
                replies.append((reply["type"], reply["channel"]))
        finally:
            if done.is_set():
                pubsub.close()
            client.close()
    assert len(kit_warnings(caplog)) == 1
    # Every channel is subscribed on the new connection, the re-sent "status" last.
    assert replies == [("subscribe", b"events"), ("subscribe", b"alerts"), ("subscribe", b"status")]
    assert stub.subscribes >= 3


# A health check every second; each test marks it due, so the next read sends it at once.
_HEALTH_CHECKED = "?health_check_interval=1"


async def _next_message(read: Callable[[], Awaitable[Any]]) -> Any:
    """The next message ``read`` returns, past the replies a pub/sub hands back as ``None``."""
    for _ in range(3):
        if (message := await read()) is not None:
            return message
    return None


async def test_sync_a_pubsub_health_check_reconnect_whose_handshake_drops_still_delivers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url + _HEALTH_CHECKED, retry_attempts=2)
        reconnects: list[None] = []
        pubsub = client.pubsub(on_reconnect=lambda: reconnects.append(None))
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            connection = pubsub.connection
            assert connection is not None
            connection.disconnect()
            connection.next_health_check = 0
            counter = count_sync_connects(monkeypatch)
            stub.drop_next(1)
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                resubscribed = await asyncio.to_thread(pubsub.get_message, timeout=1)
            stub.publish(b"events", b"after")
            message = await _next_message(lambda: asyncio.to_thread(pubsub.get_message, timeout=1))
        finally:
            pubsub.close()
            client.close()
    assert resubscribed is not None
    assert resubscribed["type"] == "subscribe"
    assert message is not None
    assert (message["type"], message["data"]) == ("message", b"after")
    warnings = kit_warnings(caplog)
    assert len(warnings) == 1
    assert HANDSHAKE_DROP in warnings[0]
    assert counter.calls == 2
    assert reconnects == [None]


async def test_async_a_pubsub_health_check_reconnect_whose_handshake_drops_still_delivers(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url + _HEALTH_CHECKED, retry_attempts=2)
        reconnects: list[None] = []
        pubsub = client.pubsub(on_reconnect=lambda: reconnects.append(None))
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            connection = pubsub.connection
            assert connection is not None
            await connection.disconnect()
            connection.next_health_check = 0
            counter = count_async_connects(monkeypatch)
            stub.drop_next(1)
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                resubscribed = await pubsub.get_message(timeout=1)
            stub.publish(b"events", b"after")
            message = await _next_message(lambda: pubsub.get_message(timeout=1))
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert resubscribed is not None
    assert resubscribed["type"] == "subscribe"
    assert message is not None
    assert (message["type"], message["data"]) == ("message", b"after")
    warnings = kit_warnings(caplog)
    assert len(warnings) == 1
    assert HANDSHAKE_DROP in warnings[0]
    assert counter.calls == 2
    assert reconnects == [None]


async def test_sync_a_pubsub_health_check_reconnect_gives_up_on_an_exhausted_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url + _HEALTH_CHECKED, retry_attempts=0)
        pubsub = client.pubsub()
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            connection = pubsub.connection
            assert connection is not None
            connection.disconnect()
            connection.next_health_check = 0
            counter = count_sync_connects(monkeypatch)
            stub.drop_next(1)
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
                await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert kit_warnings(caplog) == []
    assert counter.calls == 1


async def test_async_a_pubsub_health_check_reconnect_gives_up_on_an_exhausted_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url + _HEALTH_CHECKED, retry_attempts=0)
        pubsub = client.pubsub()
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            connection = pubsub.connection
            assert connection is not None
            await connection.disconnect()
            connection.next_health_check = 0
            counter = count_async_connects(monkeypatch)
            stub.drop_next(1)
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError):
                await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert kit_warnings(caplog) == []
    assert counter.calls == 1


@pytest.mark.parametrize(("retry_attempts", "warnings"), [(1, 1), (0, 0)], ids=["retried", "no-budget"])
async def test_sync_a_command_sent_on_a_dropped_socket_connects_under_the_retry(
    retry_attempts: int, warnings: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        connection = sync_kit_connection(stub.url, **kit_options(retry_attempts, sync=True))
        try:
            await asyncio.to_thread(connection.connect)
            connection.disconnect()
            counter = count_sync_connects(monkeypatch)
            stub.drop_next(1)

            def ping() -> Any:
                connection.send_command("PING")
                return connection.read_response()

            with caplog.at_level(logging.WARNING, logger=LOGGER):
                if retry_attempts:
                    reply = await asyncio.to_thread(ping)
                else:
                    with pytest.raises(RedisConnectionError, match=HANDSHAKE_DROP):
                        await asyncio.to_thread(ping)
        finally:
            connection.disconnect()
    if retry_attempts:
        assert reply == b"PONG"
    logged = kit_warnings(caplog)
    assert len(logged) == warnings
    assert all(HANDSHAKE_DROP in warning for warning in logged)
    assert counter.calls == 1 + warnings


@pytest.mark.parametrize(("retry_attempts", "warnings"), [(1, 1), (0, 0)], ids=["retried", "no-budget"])
async def test_async_a_command_sent_on_a_dropped_socket_connects_under_the_retry(
    retry_attempts: int, warnings: int, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        connection = async_kit_connection(stub.url, **kit_options(retry_attempts, sync=False))
        try:
            await connection.connect()
            await connection.disconnect()
            counter = count_async_connects(monkeypatch)
            stub.drop_next(1)

            async def ping() -> Any:
                await connection.send_command("PING")
                return await connection.read_response()

            with caplog.at_level(logging.WARNING, logger=LOGGER):
                if retry_attempts:
                    reply = await ping()
                else:
                    with pytest.raises(RedisConnectionError, match=HANDSHAKE_DROP):
                        await ping()
        finally:
            await connection.disconnect()
    if retry_attempts:
        assert reply == b"PONG"
    logged = kit_warnings(caplog)
    assert len(logged) == warnings
    assert all(HANDSHAKE_DROP in warning for warning in logged)
    assert counter.calls == 1 + warnings
