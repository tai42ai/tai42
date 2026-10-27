"""Real sockets: a fresh connection whose handshake drops.

A connect is one attempt of the connection's retry over the socket open and the handshake
together, so a fresh connection whose handshake drops is retried and logged on every path
that opens one: pool checkout, pipeline, first subscribe and pub/sub reconnect. The socket
cases dial loopback stubs only.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

pytest.importorskip("redis")

from redis.exceptions import ConnectionError as RedisConnectionError

from tai42_kit.clients.impl import redis as redis_mod

from ._redis_log import LOGGER, kit_warnings, on_warning
from ._redis_socket_opens import count_async_connects, count_sync_connects
from ._redis_stub import HANDSHAKE_DROP, serving_stub


async def test_async_a_pool_checkout_whose_handshake_drops_is_retried_with_warnings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        stub.drop_next(2)
        counter = count_async_connects(monkeypatch)
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=3, env_prefix="X_")
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                assert await client.ping() is True
        finally:
            await client.aclose()
    assert counter.calls == 3
    warnings = kit_warnings(caplog)
    assert len(warnings) == 2
    assert warnings[0].startswith(f"Redis connection X_REDIS_URL failed (attempt 1 of 4): {HANDSHAKE_DROP}")


async def test_sync_a_pool_checkout_whose_handshake_drops_is_retried_with_warnings(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        stub.drop_next(2)
        counter = count_sync_connects(monkeypatch)
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=3, env_prefix="X_")
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                assert await asyncio.to_thread(client.ping) is True
        finally:
            client.close()
    assert counter.calls == 3
    warnings = kit_warnings(caplog)
    assert len(warnings) == 2
    assert warnings[0].startswith(f"Redis connection X_REDIS_URL failed (attempt 1 of 4): {HANDSHAKE_DROP}")


async def test_async_handshake_drops_past_the_budget_raise_the_last_drop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        stub.drop_next(3)
        counter = count_async_connects(monkeypatch)
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as raised:
                await client.ping()
        finally:
            await client.aclose()
    assert str(raised.value) == HANDSHAKE_DROP
    assert counter.calls == 3
    assert len(kit_warnings(caplog)) == 2


async def test_sync_handshake_drops_past_the_budget_raise_the_last_drop(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        stub.drop_next(3)
        counter = count_sync_connects(monkeypatch)
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as raised:
                await asyncio.to_thread(client.ping)
        finally:
            client.close()
    assert str(raised.value) == HANDSHAKE_DROP
    assert counter.calls == 3
    assert len(kit_warnings(caplog)) == 2


async def test_async_a_handshake_drop_with_no_retry_budget_raises_at_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        stub.drop_next(1)
        counter = count_async_connects(monkeypatch)
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=0)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as raised:
                await client.ping()
        finally:
            await client.aclose()
    assert str(raised.value) == HANDSHAKE_DROP
    assert counter.calls == 1
    assert kit_warnings(caplog) == []


async def test_sync_a_handshake_drop_with_no_retry_budget_raises_at_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async with serving_stub() as stub:
        stub.drop_next(1)
        counter = count_sync_connects(monkeypatch)
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=0)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(RedisConnectionError) as raised:
                await asyncio.to_thread(client.ping)
        finally:
            client.close()
    assert str(raised.value) == HANDSHAKE_DROP
    assert counter.calls == 1
    assert kit_warnings(caplog) == []


async def test_async_a_pipeline_whose_handshake_drops_is_retried(caplog: pytest.LogCaptureFixture) -> None:
    async with serving_stub() as stub:
        stub.drop_next(1)
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                async with client.pipeline(transaction=False) as pipe:
                    pipe.set("k", "v")
                    pipe.get("k")
                    replies = await pipe.execute()
        finally:
            await client.aclose()
    assert replies == [True, b"OK"]
    assert len(kit_warnings(caplog)) == 1


async def test_sync_a_pipeline_whose_handshake_drops_is_retried(caplog: pytest.LogCaptureFixture) -> None:
    async with serving_stub() as stub:
        stub.drop_next(1)
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)

        def run_pipeline() -> list[Any]:
            with client.pipeline(transaction=False) as pipe:
                pipe.set("k", "v")
                pipe.get("k")
                return pipe.execute()

        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                replies = await asyncio.to_thread(run_pipeline)
        finally:
            client.close()
    assert replies == [True, b"OK"]
    assert len(kit_warnings(caplog)) == 1


async def test_async_a_first_subscribe_whose_handshake_drops_is_retried(caplog: pytest.LogCaptureFixture) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        stub.drop_next(1)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                await pubsub.subscribe("events")
            subscribed = await pubsub.get_message(timeout=1)
            stub.publish(b"events", b"hello")
            message = await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert subscribed is not None
    assert subscribed["type"] == "subscribe"
    assert message is not None
    assert (message["type"], message["data"]) == ("message", b"hello")
    assert len(kit_warnings(caplog)) == 1


async def test_sync_a_first_subscribe_whose_handshake_drops_is_retried(caplog: pytest.LogCaptureFixture) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        pubsub = client.pubsub()
        stub.drop_next(1)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                await asyncio.to_thread(pubsub.subscribe, "events")
            subscribed = await asyncio.to_thread(pubsub.get_message, timeout=1)
            stub.publish(b"events", b"hello")
            message = await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert subscribed is not None
    assert subscribed["type"] == "subscribe"
    assert message is not None
    assert (message["type"], message["data"]) == ("message", b"hello")
    assert len(kit_warnings(caplog)) == 1


async def test_async_a_pubsub_reconnect_whose_handshake_drops_still_delivers(caplog: pytest.LogCaptureFixture) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.RedisClient()._create(url=stub.url, retry_attempts=2)
        reconnects: list[None] = []
        pubsub = client.pubsub(on_reconnect=lambda: reconnects.append(None))
        try:
            await pubsub.subscribe("events")
            await pubsub.get_message(timeout=1)
            stub.drop()
            stub.drop_next(1)
            loop = asyncio.get_running_loop()
            # The reconnect is refused once, then its handshake drops once, then it connects.
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                async with on_warning(1, lambda: stub.come_back(loop)):
                    resubscribed = await pubsub.get_message(timeout=1)
            stub.publish(b"events", b"after")
            message = await pubsub.get_message(timeout=1)
        finally:
            await pubsub.aclose()
            await client.aclose()
    assert resubscribed is not None
    assert resubscribed["type"] == "subscribe"
    assert message is not None
    assert (message["type"], message["data"]) == ("message", b"after")
    assert reconnects == [None]
    assert any(HANDSHAKE_DROP in warning for warning in kit_warnings(caplog))


async def test_sync_a_pubsub_reconnect_whose_handshake_drops_still_delivers(caplog: pytest.LogCaptureFixture) -> None:
    async with serving_stub() as stub:
        client = await redis_mod.SyncRedisClient()._create(url=stub.url, retry_attempts=2)
        reconnects: list[None] = []
        pubsub = client.pubsub(on_reconnect=lambda: reconnects.append(None))
        try:
            await asyncio.to_thread(pubsub.subscribe, "events")
            await asyncio.to_thread(pubsub.get_message, timeout=1)
            stub.drop()
            stub.drop_next(1)
            loop = asyncio.get_running_loop()
            # The reconnect is refused once, then its handshake drops once, then it connects.
            with caplog.at_level(logging.WARNING, logger=LOGGER):
                async with on_warning(1, lambda: stub.come_back(loop)):
                    resubscribed = await asyncio.to_thread(pubsub.get_message, timeout=1)
            stub.publish(b"events", b"after")
            message = await asyncio.to_thread(pubsub.get_message, timeout=1)
        finally:
            pubsub.close()
            client.close()
    assert resubscribed is not None
    assert resubscribed["type"] == "subscribe"
    assert message is not None
    assert (message["type"], message["data"]) == ("message", b"after")
    assert reconnects == [None]
    assert any(HANDSHAKE_DROP in warning for warning in kit_warnings(caplog))
