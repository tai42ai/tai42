"""Real sockets: a connection the server refuses for its password.

A refused password is no dropped connection: no later attempt carries another password,
so the refusal raises at once, whatever the retry budget, and logs no retry. The cases
dial a loopback stub that refuses every password but its own, on the ``AUTH`` handshake
and on the ``HELLO`` one.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

pytest.importorskip("redis")

from redis.exceptions import AuthenticationError

from tai42_kit.clients.impl import redis as redis_mod

from ._redis_log import LOGGER, kit_warnings
from ._redis_socket_opens import count_async_connects, count_sync_connects
from ._redis_stub import REFUSED_PASSWORD, serving_stub

_HANDSHAKES = {"auth": "", "hello": "?protocol=3"}


def _wrong_password_url(port: int, handshake: str) -> str:
    return f"redis://:wrong@127.0.0.1:{port}/0{_HANDSHAKES[handshake]}"


@pytest.mark.parametrize("handshake", sorted(_HANDSHAKES))
async def test_async_a_refused_password_raises_at_once_whatever_the_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, handshake: str
) -> None:
    async with serving_stub(password="right") as stub:
        counter = count_async_connects(monkeypatch)
        client = await redis_mod.RedisClient()._create(url=_wrong_password_url(stub.port, handshake), retry_attempts=3)
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(AuthenticationError) as raised:
                await client.ping()
        finally:
            await client.aclose()
    assert str(raised.value) == REFUSED_PASSWORD
    assert (counter.calls, stub.auths) == (1, 1)
    assert kit_warnings(caplog) == []


@pytest.mark.parametrize("handshake", sorted(_HANDSHAKES))
async def test_sync_a_refused_password_raises_at_once_whatever_the_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, handshake: str
) -> None:
    async with serving_stub(password="right") as stub:
        counter = count_sync_connects(monkeypatch)
        client = await redis_mod.SyncRedisClient()._create(
            url=_wrong_password_url(stub.port, handshake), retry_attempts=3
        )
        try:
            with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(AuthenticationError) as raised:
                await asyncio.to_thread(client.ping)
        finally:
            client.close()
    assert str(raised.value) == REFUSED_PASSWORD
    assert (counter.calls, stub.auths) == (1, 1)
    assert kit_warnings(caplog) == []


async def test_async_a_pipeline_on_a_refused_password_raises_at_once(caplog: pytest.LogCaptureFixture) -> None:
    async with serving_stub(password="right") as stub:
        client = await redis_mod.RedisClient()._create(url=_wrong_password_url(stub.port, "auth"), retry_attempts=3)
        try:
            async with client.pipeline() as pipe:
                pipe.ping()
                with caplog.at_level(logging.WARNING, logger=LOGGER), pytest.raises(AuthenticationError):
                    await pipe.execute()
        finally:
            await client.aclose()
    assert stub.auths == 1
    assert kit_warnings(caplog) == []
