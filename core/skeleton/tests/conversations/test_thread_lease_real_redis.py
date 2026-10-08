"""The thread lease's release announcement over a real Redis.

Opt-in: set ``TAI42_SKELETON_REAL_REDIS_URL``. The conversations Redis is configured with a short
read timeout, so a waiter that waits longer than it proves the release listener's read does not
expire while the holder runs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid

import pytest
from redis.asyncio import Redis

from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.thread_lease import ThreadTurnLease

pytestmark = pytest.mark.integration

_READ_TIMEOUT_SECONDS = 0.5


@pytest.fixture
def redis_url(monkeypatch: pytest.MonkeyPatch) -> str:
    url = os.environ.get("TAI42_SKELETON_REAL_REDIS_URL")
    if not url:
        pytest.skip("real-Redis thread lease test is opt-in: set TAI42_SKELETON_REAL_REDIS_URL")
    monkeypatch.setenv("CONVERSATIONS_REDIS_URL", url)
    monkeypatch.setenv("CONVERSATIONS_SOCKET_TIMEOUT", str(_READ_TIMEOUT_SECONDS))
    monkeypatch.setenv("CONVERSATIONS_PREFIX", f"lease-test-{uuid.uuid4().hex}")
    return url


async def test_a_waiter_outlasting_the_read_timeout_takes_the_lease_at_the_release(
    redis_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    settings = ConversationsSettings()
    holder, waiter = ThreadTurnLease(lambda: settings), ThreadTurnLease(lambda: settings)
    held, release, waiting = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def _hold() -> float:
        async with holder.held("thread-1"):
            held.set()
            await release.wait()
            return time.perf_counter()

    async def _take() -> float:
        waiting.set()
        async with waiter.held("thread-1"):
            return time.perf_counter()

    holding = asyncio.create_task(_hold())
    await asyncio.wait_for(held.wait(), 5)
    taking = asyncio.create_task(_take())
    await asyncio.wait_for(waiting.wait(), 5)
    with caplog.at_level(logging.ERROR, logger="tai42_skeleton.conversations.thread_lease"):
        await asyncio.sleep(_READ_TIMEOUT_SECONDS * 3)
        assert not taking.done()
        release.set()
        released_at = await holding
        acquired_at = await asyncio.wait_for(taking, 5)
    assert acquired_at - released_at < 0.1
    assert "lost its subscription" not in caplog.text


async def test_a_lapsed_lease_is_taken_when_it_expires(redis_url: str) -> None:
    settings = ConversationsSettings()
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        await client.set(settings.thread_lease_key("thread-2"), "crashed-holder", px=400)
        started = time.perf_counter()
        async with ThreadTurnLease(lambda: settings).held("thread-2"):
            waited = time.perf_counter() - started
            assert await client.get(settings.thread_lease_key("thread-2")) != "crashed-holder"
        assert 0.3 < waited < 2
    finally:
        await client.aclose()
