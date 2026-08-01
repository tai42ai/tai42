"""The correlation store: ts→callback_url with budget TTL, and event dedupe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tai42_contract.channels import ChannelDeliveryError

from tai42_channel_slack.correlation import (
    DEDUPE_TTL_SECONDS,
    claim_dedupe,
    delete_correlation,
    get_callback_url,
    release_dedupe,
    store_correlation,
)

pytestmark = pytest.mark.usefixtures("slack_env")

_CALLBACK = "http://gateway/api/interactions/callback/ticket-9"


def _deadline(seconds: float = 300) -> datetime:
    return datetime.now(UTC) + timedelta(seconds=seconds)


async def test_store_correlation_writes_value_and_positive_ttl(fake_redis):
    timeout_at = datetime.now(UTC) + timedelta(seconds=120)

    await store_correlation("11.22", _CALLBACK, timeout_at)

    assert fake_redis.store["channel:slack:corr:11.22"] == _CALLBACK
    ttl = fake_redis.ttls["channel:slack:corr:11.22"]
    assert ttl is not None
    assert 0 < ttl <= 120


async def test_store_correlation_expired_budget_raises_and_writes_nothing(fake_redis):
    timeout_at = datetime.now(UTC) - timedelta(seconds=1)

    with pytest.raises(ChannelDeliveryError, match="budget already expired"):
        await store_correlation("11.22", _CALLBACK, timeout_at)

    assert fake_redis.store == {}


async def test_get_callback_url_hit_and_miss(fake_redis):
    await store_correlation("11.22", _CALLBACK, datetime.now(UTC) + timedelta(seconds=60))

    assert await get_callback_url("11.22") == _CALLBACK
    assert await get_callback_url("99.99") is None


async def test_delete_correlation_removes_mapping(fake_redis):
    await store_correlation("11.22", _CALLBACK, datetime.now(UTC) + timedelta(seconds=60))

    await delete_correlation("11.22")

    assert await get_callback_url("11.22") is None


async def test_claim_dedupe_first_wins_second_loses(fake_redis):
    assert await claim_dedupe("Ev1") is True
    assert await claim_dedupe("Ev1") is False
    assert fake_redis.ttls["channel:slack:event:Ev1"] == DEDUPE_TTL_SECONDS


async def test_release_dedupe_allows_reclaim(fake_redis):
    assert await claim_dedupe("Ev1") is True
    await release_dedupe("Ev1")
    assert await claim_dedupe("Ev1") is True


async def test_missing_redis_url_raises_on_every_store_function(monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_SLACK_REDIS_URL")
    monkeypatch.delenv("TAI_DEFAULT_REDIS_URL", raising=False)
    reset_all_settings()

    for call in (
        store_correlation("11.22", _CALLBACK, _deadline()),
        get_callback_url("11.22"),
        delete_correlation("11.22"),
        claim_dedupe("Ev1"),
        release_dedupe("Ev1"),
    ):
        with pytest.raises(ValueError, match="CHANNEL_SLACK_REDIS_URL"):
            await call


async def test_specific_redis_url_configures_the_store(fake_redis):
    # The store URL is set by slack_env — a store call goes through, no config error.
    assert await get_callback_url("nope") is None


async def test_default_namespace_redis_url_configures_the_store(fake_redis, monkeypatch: pytest.MonkeyPatch):
    from tai42_kit.settings import reset_all_settings

    monkeypatch.delenv("CHANNEL_SLACK_REDIS_URL")
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6379/0")
    reset_all_settings()

    # The resolved setting falls back to the default namespace, so the gate passes.
    assert await get_callback_url("nope") is None
