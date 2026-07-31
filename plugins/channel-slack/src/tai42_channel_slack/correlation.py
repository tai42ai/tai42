"""Plugin-owned correlation store over kit's pooled ``RedisClient``.

Two key families:

* ``channel:slack:corr:<ts>`` -> callback_url. Written after a successful
  ``chat.postMessage`` (the posted ``ts`` is the thread anchor every in-thread
  reply carries as ``thread_ts``). TTL = the question's remaining budget; deleted
  after a successful forward.
* ``channel:slack:event:<event_id>`` -> ``"1"``. Events API dedupe claim
  (SET NX EX) whose TTL outlives Slack's retry ladder. A handler that fails AFTER
  claiming releases the claim before re-raising, so the retry reprocesses the
  event instead of vanishing behind the dedupe key.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import cast

from tai42_contract.app import tai42_app
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_slack.settings import slack_redis_settings

_CORR_KEY = "channel:slack:corr:{ts}"
_DEDUPE_KEY = "channel:slack:event:{event_id}"

# Outlives Slack's full retry ladder (immediate, ~1 min, ~5 min) with margin.
DEDUPE_TTL_SECONDS = 900


def remaining_seconds(timeout_at: datetime) -> int:
    """Whole seconds left in the question budget (ceil; <= 0 means expired)."""
    return math.ceil((timeout_at - datetime.now(UTC)).total_seconds())


async def store_correlation(ts: str, callback_url: str, timeout_at: datetime) -> None:
    """Map the posted message's ``ts`` to its callback URL, TTL = budget.

    Raises :class:`ChannelDeliveryError` when the budget is already spent — never
    a non-positive-TTL write.
    """
    ttl = remaining_seconds(timeout_at)
    if ttl <= 0:
        raise ChannelDeliveryError(f"question budget already expired (timeout_at={timeout_at.isoformat()})")
    async with tai42_app.clients.client_ctx(RedisClient, slack_redis_settings()) as redis:
        await redis.set(_CORR_KEY.format(ts=ts), callback_url, ex=ttl)


async def get_callback_url(thread_ts: str) -> str | None:
    """The callback URL of the pending question anchoring ``thread_ts``, or ``None``
    (no pending question — the inbound handler treats it as ignorable chatter)."""
    async with tai42_app.clients.client_ctx(RedisClient, slack_redis_settings()) as redis:
        # decode_responses=True (the default), so GET returns str; cast confines the stub type.
        return cast("str | None", await redis.get(_CORR_KEY.format(ts=thread_ts)))


async def delete_correlation(thread_ts: str) -> None:
    """Drop the mapping after a successful forward (the callback door's single-use
    claim is the real idempotency guard; this just stops later thread chatter
    re-forwarding)."""
    async with tai42_app.clients.client_ctx(RedisClient, slack_redis_settings()) as redis:
        await redis.delete(_CORR_KEY.format(ts=thread_ts))


async def claim_dedupe(event_id: str) -> bool:
    """Atomically claim ``event_id`` (SET NX EX). ``False`` = already processed
    or currently in flight — the caller acks the retry without reprocessing."""
    async with tai42_app.clients.client_ctx(RedisClient, slack_redis_settings()) as redis:
        return bool(await redis.set(_DEDUPE_KEY.format(event_id=event_id), "1", ex=DEDUPE_TTL_SECONDS, nx=True))


async def release_dedupe(event_id: str) -> None:
    """Release a claim whose processing failed, so Slack's retry reprocesses
    the event instead of hitting the duplicate ack."""
    async with tai42_app.clients.client_ctx(RedisClient, slack_redis_settings()) as redis:
        await redis.delete(_DEDUPE_KEY.format(event_id=event_id))
