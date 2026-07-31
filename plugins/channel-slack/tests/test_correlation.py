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
