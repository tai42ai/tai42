"""Real sockets: a pub/sub's reconnect hook.

The hook runs once per reconnect, right after the re-subscribe, on every door that
reconnects the pub/sub; an error it raises reaches the read or command that met the drop
as it is, with the link left up and subscribed. The socket cases dial loopback stubs only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

import pytest

pytest.importorskip("redis")

import redis
from redis.asyncio import ConnectionPool as AsyncConnectionPool
from redis.asyncio.retry import Retry as AsyncRetry
from redis.backoff import NoBackoff
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.retry import Retry as SyncRetry

from tai42_kit.clients.impl import redis as redis_mod

from ._redis_log import LOGGER, kit_warnings, on_warning
from ._redis_socket_opens import count_async_connects, count_sync_connects
from ._redis_stub import HANDSHAKE_DROP, serving_stub


async def test_async_a_pubsub_runs_its_reconnect_hook_after_each_re_subscribe() -> None:
    async with serving_stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        reconnects: list[None] = []
        pubsub = client.pubsub(on_reconnect=lambda: reconnects.append(None))
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            assert reconnects == []
            stub.drop()
            loop = asyncio.get_running_loop()
            async with on_warning(2, lambda: stub.come_back(loop)):
                message = await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert message is not None
    assert message["type"] == "subscribe"
    # Once, on the reconnect, which subscribed "events" again.
    assert reconnects == [None]
    assert stub.subscribes == 2


async def test_sync_a_pubsub_runs_its_reconnect_hook_after_each_re_subscribe() -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        reconnects: list[None] = []
        pubsub = client.pubsub(on_reconnect=lambda: reconnects.append(None))
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            assert reconnects == []
            stub.drop()
            loop = asyncio.get_running_loop()
            async with on_warning(2, lambda: stub.come_back(loop)):
                message = await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert message is not None
    assert message["type"] == "subscribe"
    assert reconnects == [None]
    assert stub.subscribes == 2


_HOOK_FAILURE = "the hook's own client gave up"


class _Hook:
    """A pub/sub reconnect hook that counts its calls and raises when told to."""

    def __init__(self, *, raises: bool) -> None:
        self.calls = 0
        self.raises = raises
        self.errors: list[BaseException] = []

    def __call__(self) -> None:
        self.calls += 1
        if self.raises:
            error = RedisConnectionError(_HOOK_FAILURE)
            self.errors.append(error)
            raise error


async def test_async_a_reconnect_hook_that_raises_runs_once_and_leaves_the_link_subscribed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        counter = count_async_connects(monkeypatch)
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=3)
        hook = _Hook(raises=True)
        pubsub = client.pubsub(on_reconnect=hook)
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            stub.drop()
            stub.come_back(asyncio.get_running_loop())
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as raised:
                await pubsub.get_message(timeout=1)
            assert pubsub.connection is not None
            connected = pubsub.connection.is_connected
            resubscribed = await pubsub.get_message(timeout=1)
            stub.publish(b"events", b"after")
            message = await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert str(raised.value) == _HOOK_FAILURE
    assert hook.calls == 1
    assert kit_warnings(caplog) == []
    assert counter.calls == 2
    assert connected is True
    assert resubscribed is not None
    assert resubscribed["type"] == "subscribe"
    assert message is not None
    assert (message["type"], message["data"]) == ("message", b"after")


async def test_sync_a_reconnect_hook_that_raises_runs_once_and_leaves_the_link_subscribed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        counter = count_sync_connects(monkeypatch)
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=3)
        hook = _Hook(raises=True)
        pubsub = client.pubsub(on_reconnect=hook)
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            stub.drop()
            stub.come_back(asyncio.get_running_loop())
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as raised:
                await asyncio.to_thread(pubsub.get_message, timeout=1)
            assert pubsub.connection is not None
            connected = pubsub.connection._sock is not None
            resubscribed = await asyncio.to_thread(pubsub.get_message, timeout=1)
            stub.publish(b"events", b"after")
            message = await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert str(raised.value) == _HOOK_FAILURE
    assert hook.calls == 1
    assert kit_warnings(caplog) == []
    assert counter.calls == 2
    assert connected is True
    assert resubscribed is not None
    assert resubscribed["type"] == "subscribe"
    assert message is not None
    assert (message["type"], message["data"]) == ("message", b"after")


# The read that meets the drop logs it when its reconnect returns; a reconnect that raises
# (the hook's error) logs nothing for it. Each of the reconnect's two handshake drops logs one.
@pytest.mark.parametrize(("raises", "warnings"), [(False, 3), (True, 2)], ids=["records", "raises"])
async def test_async_the_reconnect_hook_runs_once_after_a_retried_reconnect(
    raises: bool, warnings: int, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=3)
        hook = _Hook(raises=raises)
        pubsub = client.pubsub(on_reconnect=hook)
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            stub.drop()
            stub.drop_next(2)
            stub.come_back(asyncio.get_running_loop())
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                if raises:
                    with pytest.raises(RedisConnectionError, match=_HOOK_FAILURE):
                        await pubsub.get_message(timeout=1)
                else:
                    resubscribed = await pubsub.get_message(timeout=1)
                    assert resubscribed is not None
                    assert resubscribed["type"] == "subscribe"
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert hook.calls == 1
    logged = kit_warnings(caplog)
    assert len(logged) == warnings
    assert all(HANDSHAKE_DROP in warning for warning in logged)


# The read that meets the drop logs it when its reconnect returns; a reconnect that raises
# (the hook's error) logs nothing for it. Each of the reconnect's two handshake drops logs one.
@pytest.mark.parametrize(("raises", "warnings"), [(False, 3), (True, 2)], ids=["records", "raises"])
async def test_sync_the_reconnect_hook_runs_once_after_a_retried_reconnect(
    raises: bool, warnings: int, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=3)
        hook = _Hook(raises=raises)
        pubsub = client.pubsub(on_reconnect=hook)
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            stub.drop()
            stub.drop_next(2)
            stub.come_back(asyncio.get_running_loop())
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                if raises:
                    with pytest.raises(RedisConnectionError, match=_HOOK_FAILURE):
                        await asyncio.to_thread(pubsub.get_message, timeout=1)
                else:
                    resubscribed = await asyncio.to_thread(pubsub.get_message, timeout=1)
                    assert resubscribed is not None
                    assert resubscribed["type"] == "subscribe"
        finally:
            pubsub.close()
            client.close()
    assert hook.calls == 1
    logged = kit_warnings(caplog)
    assert len(logged) == warnings
    assert all(HANDSHAKE_DROP in warning for warning in logged)


async def test_async_a_kit_pubsub_on_a_connection_that_is_not_the_kits_raises_at_its_first_reconnect() -> None:
    async with serving_stub() as stub:
        pool = AsyncConnectionPool.from_url(
            stub.url, retry=AsyncRetry(NoBackoff(), 1), retry_on_error=[RedisConnectionError]
        )
        pubsub = redis_mod.KitAsyncPubSub(pool, on_reconnect=_Hook(raises=False))
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            stub.drop()
            stub.come_back(asyncio.get_running_loop())
            with pytest.raises(TypeError, match="run_unretried"):
                await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await pool.aclose()


async def test_sync_a_kit_pubsub_on_a_connection_that_is_not_the_kits_raises_at_its_first_reconnect() -> None:
    async with serving_stub() as stub:
        pool = redis.ConnectionPool.from_url(
            stub.url, retry=SyncRetry(NoBackoff(), 1), retry_on_error=[RedisConnectionError]
        )
        pubsub = redis_mod.ReentrantPubSub(pool, on_reconnect=_Hook(raises=False))
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            stub.drop()
            stub.come_back(asyncio.get_running_loop())
            with pytest.raises(TypeError, match="run_unretried"):
                await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            pool.disconnect()


async def test_sync_a_reconnect_inside_a_read_runs_a_raising_hook_once_and_leaves_the_link_up(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=3)
        hook = _Hook(raises=True)
        pubsub = client.pubsub(on_reconnect=hook)
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            # The pool's disconnect closes the pub/sub's socket; the read's own check
            # reconnects it inside the read's retried operation.
            client.connection_pool.disconnect()
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as raised:
                await asyncio.to_thread(pubsub.get_message, timeout=1)
            assert pubsub.connection is not None
            connected = pubsub.connection._sock is not None
        finally:
            pubsub.close()
            client.close()
    assert hook.calls == 1
    assert raised.value is hook.errors[0]
    assert raised.value.__context__ is None
    assert connected is True
    assert kit_warnings(caplog) == []


async def _async_resubscribe_after_a_pool_disconnect(hook: _Hook) -> tuple[Any, list[Any], bool]:
    """Subscribe, read, drop the pool's sockets, then subscribe again; return the error, replies, link."""
    async with serving_stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=3)
        pubsub = client.pubsub(on_reconnect=hook)
        error: Any = None
        replies: list[Any] = []
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            await cast("AsyncConnectionPool", client.connection_pool).disconnect()
            try:
                await pubsub.subscribe("alerts")
            except RedisConnectionError as raised:
                error = raised
            while (reply := await pubsub.get_message(timeout=1)) is not None:
                replies.append((reply["type"], reply["channel"]))
            assert pubsub.connection is not None
            connected = pubsub.connection.is_connected
        finally:
            await pubsub.aclose()
            await client.aclose()
    return error, replies, connected


async def _sync_resubscribe_after_a_pool_disconnect(hook: _Hook) -> tuple[Any, list[Any], bool]:
    """Subscribe, read, drop the pool's sockets, then subscribe again; return the error, replies, link."""
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=3)
        pubsub = client.pubsub(on_reconnect=hook)
        error: Any = None
        replies: list[Any] = []
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            client.connection_pool.disconnect()
            try:
                await asyncio.to_thread(pubsub.subscribe, "alerts")
            except RedisConnectionError as raised:
                error = raised
            while (reply := await asyncio.to_thread(pubsub.get_message, timeout=1)) is not None:
                replies.append((reply["type"], reply["channel"]))
            assert pubsub.connection is not None
            connected = pubsub.connection._sock is not None
        finally:
            pubsub.close()
            client.close()
    return error, replies, connected


# The command that sends on the closed socket reconnects it through connect(), from inside
# its own retried operation.
_RESUBSCRIBED = [("subscribe", b"events"), ("subscribe", b"alerts")]


async def test_async_a_reconnect_inside_a_command_runs_the_hook_once() -> None:
    hook = _Hook(raises=False)
    error, replies, connected = await _async_resubscribe_after_a_pool_disconnect(hook)
    assert error is None
    assert hook.calls == 1
    assert replies == _RESUBSCRIBED
    assert connected is True


async def test_sync_a_reconnect_inside_a_command_runs_the_hook_once() -> None:
    hook = _Hook(raises=False)
    error, replies, connected = await _sync_resubscribe_after_a_pool_disconnect(hook)
    assert error is None
    assert hook.calls == 1
    assert replies == _RESUBSCRIBED
    assert connected is True


async def test_async_a_reconnect_inside_a_command_runs_a_raising_hook_once_and_leaves_the_link_subscribed() -> None:
    hook = _Hook(raises=True)
    error, replies, connected = await _async_resubscribe_after_a_pool_disconnect(hook)
    assert hook.calls == 1
    assert error is hook.errors[0]
    assert error.__context__ is None
    # The hook's error ends the command before its own send; the reconnect re-subscribed "events".
    assert replies == [("subscribe", b"events")]
    assert connected is True


async def test_sync_a_reconnect_inside_a_command_runs_a_raising_hook_once_and_leaves_the_link_subscribed() -> None:
    hook = _Hook(raises=True)
    error, replies, connected = await _sync_resubscribe_after_a_pool_disconnect(hook)
    assert hook.calls == 1
    assert error is hook.errors[0]
    assert error.__context__ is None
    assert replies == [("subscribe", b"events")]
    assert connected is True
