"""The Telegram correlation store — the contract :class:`CorrelationStore` port.

One Redis string key per delivered question::

    channel:telegram:corr:{message_id} -> a JSON :class:`Correlation` record

The opaque correlation key is the ForceReply anchor's ``message_id`` (as a string);
the ``message_id`` is minted by ``sendMessage``, so a delivered question and the
reply that answers it share it. The value is the delivery's
:class:`~tai42_contract.channels.Correlation` (callback_url + interaction_id +
ttl_deadline). Written by ``TelegramChannel.deliver`` after ``sendMessage`` returns,
read by the shared inbound-answer ladder to route the ForceReply answer, expired by
Redis at the question's deadline (TTL = remaining budget). Connection from
:class:`TelegramCorrelationSettings` (``CHANNEL_TELEGRAM_REDIS_URL``).

The reservation is ``SET NX``: one pending ask per ``message_id`` anchor, so a
delivery never silently overwrites a live reservation on the same anchor (a
``message_id`` is unique per send, so this never collides in practice — the NX just
makes the one-pending guarantee explicit at the port).
"""

from __future__ import annotations

import logging
from typing import cast

from tai42_contract.app import tai42_app
from tai42_contract.channels import Correlation
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_telegram.settings import TelegramCorrelationSettings, telegram_correlation_settings

logger = logging.getLogger(__name__)

_KEY_PREFIX = "channel:telegram:corr:"


def _key(correlation_key: str) -> str:
    return f"{_KEY_PREFIX}{correlation_key}"


def _redis_settings() -> TelegramCorrelationSettings:
    """The correlation-store connection, raising a clear config error when unset."""
    settings = telegram_correlation_settings()
    if not settings.redis_url:
        raise ValueError("Telegram channel correlation store is not configured: set CHANNEL_TELEGRAM_REDIS_URL.")
    return settings


def _redis_ctx():
    return tai42_app.clients.client_ctx(RedisClient, _redis_settings())


class TelegramCorrelationStore:
    """Satisfies :class:`~tai42_contract.channels.CorrelationStore` over the
    plugin-owned ``channel:telegram:corr:{message_id}`` string keys.

    Stateless — the Redis connection is read at each call so a live-reload picks up a
    rotated URL with no stale per-instance snapshot."""

    async def set_correlation(self, key: str, entry: Correlation, *, ttl_seconds: int) -> bool:
        """Reserve ``key`` for ``entry`` NX with a ``ttl_seconds`` expiry.

        Returns True when the anchor was free and is now held; False when it is
        already held. ``ttl_seconds`` (the question's remaining budget) must be
        positive — a non-positive TTL would mint a key without an expiry.
        """
        if ttl_seconds <= 0:
            raise ValueError(f"correlation TTL must be positive, got {ttl_seconds}")
        async with _redis_ctx() as r:
            stored = await r.set(_key(key), entry.model_dump_json(), nx=True, ex=ttl_seconds)
        return bool(stored)

    async def get_correlation(self, key: str) -> Correlation | None:
        """The pending question's record under ``key``, or ``None`` (unknown/expired).

        A non-destructive peek — it neither drops nor refreshes the reservation."""
        async with _redis_ctx() as r:
            # decode_responses=True on this connection, so a hit is always ``str``.
            raw = cast("str | None", await r.get(_key(key)))
        if raw is None:
            return None
        try:
            return Correlation.model_validate_json(raw)
        except ValueError:
            # A record written by the pre-migration code (a bare callback URL string, not
            # a JSON Correlation) does not parse. Tolerate it as a graceful miss so the
            # reply bridges, never a 500; never log the value (it is a callback URL).
            logger.warning(
                "telegram: correlation for key %r is not the current Correlation shape "
                "(pre-migration bare-URL record?); treating as no correlation",
                key,
            )
            return None

    async def release_correlation(self, key: str) -> None:
        """Drop any reservation under ``key``, idempotently (a no-op when free)."""
        async with _redis_ctx() as r:
            await r.delete(_key(key))


# Module-level singleton: the store is stateless, so one instance serves the deliver
# and inbound paths (the channel hands it to the shared ladder explicitly).
telegram_correlation_store = TelegramCorrelationStore()
