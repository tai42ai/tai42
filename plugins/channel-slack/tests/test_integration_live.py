"""Live integration tests against a real Slack workspace + Redis.

Run with ``pytest -m integration``. Reads ``CHANNEL_SLACK_BOT_TOKEN`` /
``CHANNEL_SLACK_DEFAULT_RECIPIENT`` / ``CHANNEL_SLACK_REDIS_URL`` from the
environment and skips cleanly when any is unset. Outbound only: the reply leg is
human-by-definition (proven by the offline flow tests), and nothing polls
``conversations.history``. The posted message stays visible in the channel.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import redis.asyncio as redis_asyncio
from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError
from tai42_kit.clients.impl.http import HttpxClient
from tai42_kit.clients.impl.redis import RedisClient
from tai42_kit.settings import reset_all_settings

from tai42_channel_slack.channel import SlackChannel

pytestmark = pytest.mark.integration

_ENV_KEYS = ("CHANNEL_SLACK_BOT_TOKEN", "CHANNEL_SLACK_DEFAULT_RECIPIENT", "CHANNEL_SLACK_REDIS_URL")

_CORR_PREFIX = "channel:slack:corr:"


def _creds() -> dict[str, str]:
    creds = {key: os.environ.get(key, "") for key in _ENV_KEYS}
    if not all(creds.values()):
        pytest.skip("CHANNEL_SLACK_* credentials not available")
    return creds


@pytest.fixture
async def live_clients(stub_app) -> AsyncIterator[redis_asyncio.Redis]:
    """Real clients wired through the same stub facets the unit tests use."""
    creds = _creds()
    reset_all_settings()
    http = httpx.AsyncClient()
    redis = redis_asyncio.Redis.from_url(creds["CHANNEL_SLACK_REDIS_URL"], decode_responses=True)
    stub_app.clients.clients[HttpxClient] = http
    stub_app.clients.clients[RedisClient] = redis
    try:
        yield redis
    finally:
        stub_app.clients.clients.pop(HttpxClient, None)
        stub_app.clients.clients.pop(RedisClient, None)
        await http.aclose()
        await redis.aclose()
        reset_all_settings()


async def test_deliver_posts_real_message(live_clients):
    redis = live_clients
    callback_url = f"http://gateway/api/interactions/callback/live-{uuid.uuid4()}"
    delivery = ChannelDelivery(
        interaction_id="live-int",
        question="[tai42-channel-slack integration test] This question expires immediately; do not reply.",
        answer_format="text",
        callback_url=callback_url,
        timeout_at=datetime.now(UTC) + timedelta(minutes=2),
    )

    await SlackChannel().deliver(delivery)

    # deliver returns None, so the posted ts is recovered by finding the
    # correlation entry that carries this delivery's unique callback URL.
    stored_key = None
    async for key in redis.scan_iter(match=f"{_CORR_PREFIX}*"):
        if await redis.get(key) == callback_url:
            stored_key = key
            break
    assert stored_key is not None, "no correlation entry found for the delivered question"
    ttl = await redis.ttl(stored_key)
    assert ttl > 0
    await redis.delete(stored_key)


async def test_ok_false_raises_live(live_clients, monkeypatch):
    # A nonexistent channel id: Slack answers HTTP 200 with ok:false and its
    # real error code, which must surface as a loud ChannelDeliveryError.
    monkeypatch.setenv("CHANNEL_SLACK_DEFAULT_RECIPIENT", "C0DOESNOTEXIST0")
    reset_all_settings()
    delivery = ChannelDelivery(
        interaction_id="live-int",
        question="[tai42-channel-slack integration test] never delivered",
        answer_format="text",
        callback_url="http://gateway/api/interactions/callback/live-neg",
        timeout_at=datetime.now(UTC) + timedelta(minutes=2),
    )

    with pytest.raises(ChannelDeliveryError, match="channel_not_found"):
        await SlackChannel().deliver(delivery)
