"""The plugin-owned message_id -> callback_url Redis store."""

from __future__ import annotations

import pytest

from tai42_channel_telegram.correlation import clear_correlation, lookup_callback_url, store_correlation

_CALLBACK = "https://example.test/api/interactions/callback/tkt"


async def test_store_writes_prefixed_key_with_ttl(fake_redis):
    await store_correlation(42, _CALLBACK, 600)
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}
    assert fake_redis.ttls == {"channel:telegram:corr:42": 600}


async def test_lookup_round_trips(fake_redis):
    await store_correlation(42, _CALLBACK, 600)
    assert await lookup_callback_url(42) == _CALLBACK


async def test_lookup_unknown_returns_none(fake_redis):
    assert await lookup_callback_url(99) is None


async def test_clear_removes_key_and_ttl(fake_redis):
    await store_correlation(42, _CALLBACK, 600)
    await clear_correlation(42)
    assert fake_redis.data == {}
    assert fake_redis.ttls == {}


@pytest.mark.parametrize("ttl", [0, -5])
async def test_non_positive_ttl_raises(fake_redis, ttl: int):
    with pytest.raises(ValueError, match="TTL must be positive"):
        await store_correlation(42, _CALLBACK, ttl)
    assert fake_redis.data == {}
