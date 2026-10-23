"""The shared reply-window store: advance a pair's close time atomically, read it back as a ``ReplyWindow``.

Every test runs on ``fakeredis`` (with Lua) and, as an ``integration`` case, on a real Redis: set
``TAI42_KIT_REAL_REDIS_URL`` to any Redis >= 6.2 (``SET ... PXAT``). Without it the real leg
skips visibly with the reason.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import fakeredis
import pytest
import redis.asyncio as aredis
from redis.exceptions import ResponseError
from tai42_contract.channels import ReplyWindow

from tai42_kit.conversations.reply_window import advance_reply_window, read_reply_window

REAL_REDIS_ENV = "TAI42_KIT_REAL_REDIS_URL"
DAY = 86_400
NOW = datetime(2026, 10, 22, 12, 0, 0, 123_000, tzinfo=UTC)


def _ms(value: datetime) -> int:
    return (value - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)


# The real leg is an ``integration`` case, so it runs in the pass that has a real Redis configured.
@pytest.fixture(params=["fake", "fake-bytes", pytest.param("real", marks=pytest.mark.integration)])
async def redis(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    if request.param == "real":
        url = os.environ.get(REAL_REDIS_ENV)
        if not url:
            pytest.skip(f"real-Redis reply-window leg is opt-in: set {REAL_REDIS_ENV} to a Redis >= 6.2")
        client = aredis.from_url(url, decode_responses=True)
    else:
        client = fakeredis.FakeAsyncRedis(decode_responses=request.param == "fake")
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def key() -> str:
    return f"test:reply-window:{uuid.uuid4().hex}"


async def _cleanup(redis: Any, key: str) -> None:
    await redis.delete(key)


def _now() -> datetime:
    """Wall-clock now: a real Redis expires a PXAT key on its own clock, so a written key must lie ahead of it."""
    return datetime.now(UTC)


async def test_advance_sets_the_close_time_and_expires_the_key_then(redis: Any, key: str) -> None:
    now = _now()
    opened_at = now - timedelta(minutes=5)
    try:
        await advance_reply_window(redis, key, opened_at=opened_at, window_seconds=DAY, now=now)
        closes_at_ms = _ms(opened_at + timedelta(seconds=DAY))
        assert int(await redis.get(key)) == closes_at_ms
        assert await redis.pexpiretime(key) == closes_at_ms
    finally:
        await _cleanup(redis, key)


async def test_an_older_opened_at_never_lowers_the_close_time(redis: Any, key: str) -> None:
    now = _now()
    newer = now - timedelta(minutes=1)
    try:
        await advance_reply_window(redis, key, opened_at=newer, window_seconds=DAY, now=now)
        await advance_reply_window(redis, key, opened_at=newer - timedelta(hours=3), window_seconds=DAY, now=now)
        assert int(await redis.get(key)) == _ms(newer + timedelta(seconds=DAY))
    finally:
        await _cleanup(redis, key)


async def test_a_newer_opened_at_moves_the_close_time_forward(redis: Any, key: str) -> None:
    now = _now()
    older = now - timedelta(hours=3)
    try:
        await advance_reply_window(redis, key, opened_at=older, window_seconds=DAY, now=now)
        await advance_reply_window(redis, key, opened_at=now, window_seconds=DAY, now=now)
        assert int(await redis.get(key)) == _ms(now + timedelta(seconds=DAY))
        assert await redis.pexpiretime(key) == _ms(now + timedelta(seconds=DAY))
    finally:
        await _cleanup(redis, key)


@pytest.mark.parametrize("late_by", [timedelta(0), timedelta(milliseconds=1), timedelta(hours=1)])
async def test_a_window_that_already_closed_writes_nothing(redis: Any, key: str, late_by: timedelta) -> None:
    now = _now()
    opened_at = now - timedelta(seconds=DAY) - late_by
    await advance_reply_window(redis, key, opened_at=opened_at, window_seconds=DAY, now=now)
    assert await redis.exists(key) == 0


async def test_read_of_a_missing_key_is_closed(redis: Any, key: str) -> None:
    assert await read_reply_window(redis, key, now=_now()) == ReplyWindow(state="closed", closes_at=None)


async def test_read_of_a_future_close_time_is_open_with_its_whole_second_close_time(redis: Any, key: str) -> None:
    now = _now()
    opened_at = now - timedelta(seconds=10, microseconds=456_000)
    try:
        await advance_reply_window(redis, key, opened_at=opened_at, window_seconds=DAY, now=now)
        window = await read_reply_window(redis, key, now=now)
        expected = (opened_at + timedelta(seconds=DAY)).replace(microsecond=0)
        assert window == ReplyWindow(state="open", closes_at=expected)
        assert window.closes_at is not None
        assert window.closes_at == expected
        assert window.closes_at.tzinfo is UTC
    finally:
        await _cleanup(redis, key)


async def test_read_of_a_close_time_at_or_before_now_is_closed(redis: Any, key: str) -> None:
    now = _now()
    try:
        await advance_reply_window(redis, key, opened_at=now, window_seconds=DAY, now=now)
        closes_at = now + timedelta(seconds=DAY)
        assert (await read_reply_window(redis, key, now=closes_at)).state == "closed"
        assert (await read_reply_window(redis, key, now=closes_at + timedelta(seconds=1))).state == "closed"
    finally:
        await _cleanup(redis, key)


async def test_read_of_a_non_integer_value_raises(redis: Any, key: str) -> None:
    try:
        await redis.set(key, "not-a-number")
        with pytest.raises(ValueError, match="reply-window"):
            await read_reply_window(redis, key, now=_now())
    finally:
        await _cleanup(redis, key)


async def test_advance_over_a_non_integer_value_raises(redis: Any, key: str) -> None:
    try:
        await redis.set(key, "not-a-number")
        with pytest.raises(ResponseError, match="not an integer"):
            await advance_reply_window(redis, key, opened_at=_now(), window_seconds=DAY, now=_now())
        assert await redis.get(key) in ("not-a-number", b"not-a-number")
    finally:
        await _cleanup(redis, key)


@pytest.mark.parametrize("window_seconds", [0, -1])
async def test_a_window_length_that_is_not_positive_is_refused(redis: Any, key: str, window_seconds: int) -> None:
    with pytest.raises(ValueError, match="window_seconds"):
        await advance_reply_window(redis, key, opened_at=NOW, window_seconds=window_seconds, now=NOW)
    assert await redis.exists(key) == 0


async def test_naive_datetimes_are_refused(redis: Any, key: str) -> None:
    naive = datetime(2026, 10, 22, 12, 0)  # the naive value under test
    with pytest.raises(ValueError, match="opened_at"):
        await advance_reply_window(redis, key, opened_at=naive, window_seconds=DAY, now=NOW)
    with pytest.raises(ValueError, match="now"):
        await advance_reply_window(redis, key, opened_at=NOW, window_seconds=DAY, now=naive)
    with pytest.raises(ValueError, match="now"):
        await read_reply_window(redis, key, now=naive)
    assert await redis.exists(key) == 0


async def test_an_offset_datetime_is_read_as_the_same_instant(redis: Any, key: str) -> None:
    now = _now()
    offset_now = now.astimezone(timezone(timedelta(hours=-5)))
    try:
        await advance_reply_window(redis, key, opened_at=offset_now, window_seconds=DAY, now=offset_now)
        assert int(await redis.get(key)) == _ms(now + timedelta(seconds=DAY))
    finally:
        await _cleanup(redis, key)


async def test_read_reports_an_open_window_only_while_its_whole_second_close_time_is_ahead(
    redis: Any, key: str
) -> None:
    now = _now().replace(microsecond=123_000)
    try:
        await advance_reply_window(redis, key, opened_at=now, window_seconds=DAY, now=now)
        whole_second_close = (now + timedelta(seconds=DAY)).replace(microsecond=0)
        before = await read_reply_window(redis, key, now=whole_second_close - timedelta(milliseconds=1))
        assert before == ReplyWindow(state="open", closes_at=whole_second_close)
        assert before.model_dump(mode="json")["closes_at"] == whole_second_close.strftime("%Y-%m-%dT%H:%M:%SZ")
        assert await read_reply_window(redis, key, now=whole_second_close) == ReplyWindow(
            state="closed", closes_at=None
        )
        assert await read_reply_window(redis, key, now=whole_second_close + timedelta(milliseconds=100)) == (
            ReplyWindow(state="closed", closes_at=None)
        )
    finally:
        await _cleanup(redis, key)
