"""The ``message_id -> callback_url`` correlation store.

One Redis string key per delivered question::

    channel:telegram:corr:{message_id} -> callback_url

Written by ``TelegramChannel.deliver`` after ``sendMessage`` returns (the
``message_id`` is minted by the send), read by the inbound door to route the
ForceReply answer, expired by Redis at the question's deadline (TTL = remaining
budget). Connection from :class:`TelegramCorrelationSettings`
(``CHANNEL_TELEGRAM_REDIS_URL``).
"""

from __future__ import annotations

from typing import cast

from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_telegram.settings import telegram_correlation_settings

_KEY_PREFIX = "channel:telegram:corr:"


def _key(message_id: int) -> str:
    return f"{_KEY_PREFIX}{message_id}"


def _redis_ctx():
    return tai42_app.clients.client_ctx(RedisClient, telegram_correlation_settings())


async def store_correlation(message_id: int, callback_url: str, ttl_seconds: int) -> None:
    """Record a sent question's mapping with the remaining budget as TTL.

    A non-positive TTL (the question already expired) raises — never a key
    without an expiry.
    """
    if ttl_seconds <= 0:
        raise ValueError(f"correlation TTL must be positive, got {ttl_seconds}")
    async with _redis_ctx() as r:
        await r.set(_key(message_id), callback_url, ex=ttl_seconds)


async def lookup_callback_url(message_id: int) -> str | None:
    """The pending question's callback_url, or ``None`` when unknown/expired."""
    async with _redis_ctx() as r:
        # decode_responses=True on this connection, so a hit is always ``str``.
        return cast("str | None", await r.get(_key(message_id)))


async def clear_correlation(message_id: int) -> None:
    """Drop a consumed mapping (the answer reached the callback door terminally)."""
    async with _redis_ctx() as r:
        await r.delete(_key(message_id))
