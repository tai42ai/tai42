"""The Telegram :class:`CorrelationStore` over the plugin-owned Redis keys.

Conformance for the contract port: a NX reserve, a non-destructive peek, and an
idempotent release round-trip on the fake redis, plus the one-pending-per-anchor
guarantee the ``set_correlation`` NX enforces.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.channels import Correlation

from tai42_channel_telegram.correlation import telegram_correlation_store as store

_CALLBACK = "https://example.test/api/interactions/callback/tkt"


def _entry(callback_url: str = _CALLBACK, interaction_id: str = "int-1") -> Correlation:
    return Correlation(
        callback_url=callback_url,
        interaction_id=interaction_id,
        ttl_deadline=datetime.now(UTC) + timedelta(minutes=10),
    )


async def test_set_writes_prefixed_key_with_ttl(fake_redis):
    stored = await store.set_correlation("42", _entry(), ttl_seconds=600)
    assert stored is True
    assert "channel:telegram:corr:42" in fake_redis.data
    assert fake_redis.ttls == {"channel:telegram:corr:42": 600}


async def test_get_round_trips_the_record(fake_redis):
    entry = _entry(interaction_id="int-99")
    await store.set_correlation("42", entry, ttl_seconds=600)
    got = await store.get_correlation("42")
    assert got is not None
    assert got.callback_url == _CALLBACK
    assert got.interaction_id == "int-99"
    # A peek is non-destructive: the reservation survives.
    assert "channel:telegram:corr:42" in fake_redis.data


async def test_get_unknown_returns_none(fake_redis):
    assert await store.get_correlation("99") is None


async def test_get_tolerates_legacy_bare_url_record(fake_redis):
    # A record written by the PRE-migration code is a bare callback URL string, not a JSON
    # Correlation. get_correlation must read it as a graceful miss (-> the reply bridges),
    # never raise a JSON/validation error.
    fake_redis.data["channel:telegram:corr:42"] = "https://example.test/api/interactions/callback/legacy"
    assert await store.get_correlation("42") is None


async def test_release_removes_key_and_is_idempotent(fake_redis):
    await store.set_correlation("42", _entry(), ttl_seconds=600)
    await store.release_correlation("42")
    assert fake_redis.data == {}
    assert fake_redis.ttls == {}
    # A second release on the now-free key is a no-op, never an error.
    await store.release_correlation("42")


async def test_set_is_nx_one_pending_per_anchor(fake_redis):
    # The first reserve holds the anchor; a second reserve on the SAME key is refused
    # (False) rather than overwriting the live reservation — the one-pending guarantee.
    assert await store.set_correlation("42", _entry(interaction_id="first"), ttl_seconds=600) is True
    assert await store.set_correlation("42", _entry(interaction_id="second"), ttl_seconds=600) is False
    held = await store.get_correlation("42")
    assert held is not None
    assert held.interaction_id == "first"


@pytest.mark.parametrize("ttl", [0, -5])
async def test_non_positive_ttl_raises(fake_redis, ttl: int):
    with pytest.raises(ValueError, match="TTL must be positive"):
        await store.set_correlation("42", _entry(), ttl_seconds=ttl)
    assert fake_redis.data == {}


async def test_missing_redis_url_raises_on_every_store_method(monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TELEGRAM_REDIS_URL")
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()

    with pytest.raises(ValueError, match="CHANNEL_TELEGRAM_REDIS_URL"):
        await store.set_correlation("42", _entry(), ttl_seconds=600)
    with pytest.raises(ValueError, match="CHANNEL_TELEGRAM_REDIS_URL"):
        await store.get_correlation("42")
    with pytest.raises(ValueError, match="CHANNEL_TELEGRAM_REDIS_URL"):
        await store.release_correlation("42")


async def test_specific_redis_url_configures_the_store(fake_redis):
    # The store URL is set by the channel_env fixture — a store call goes through
    # without a config error.
    assert await store.get_correlation("99") is None


async def test_default_namespace_redis_url_configures_the_store(fake_redis, monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_TELEGRAM_REDIS_URL")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")
    reset_all_settings()

    # The resolved setting falls back to the default namespace, so the gate passes.
    assert await store.get_correlation("99") is None
