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


async def test_missing_redis_url_raises_on_every_store_function(monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TELEGRAM_REDIS_URL")
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()

    for call in (
        store_correlation(42, _CALLBACK, 600),
        lookup_callback_url(42),
        clear_correlation(42),
    ):
        with pytest.raises(ValueError, match="CHANNEL_TELEGRAM_REDIS_URL"):
            await call


async def test_specific_redis_url_configures_the_store(fake_redis):
    # The store URL is set by the channel_env fixture — a store call goes
    # through without a config error.
    assert await lookup_callback_url(99) is None


async def test_default_namespace_redis_url_configures_the_store(fake_redis, monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TELEGRAM_REDIS_URL")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")
    reset_all_settings()

    # The resolved setting falls back to the default namespace, so the gate passes.
    assert await lookup_callback_url(99) is None
