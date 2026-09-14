"""The store's Redis connection and the record-field primitives every family shares.

The settings guard is private to this module; every other store submodule reaches
Redis through the context helpers here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from tai42_contract.app import tai42_app
from tai42_kit.clients.impl.redis import RedisClient

from tai42_channel_web.settings import WebRedisSettings, web_redis_settings


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _mint_id() -> str:
    return uuid4().hex


def _as_str(value: str | bytes) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _redis_settings() -> WebRedisSettings:
    """The plugin's store connection, raising a clear config error when unset.

    ``redis_url`` falls back to ``TAI_DEFAULT_REDIS_URL`` via the mixin; the guard
    fires only when NEITHER the channel's own URL nor the shared default is set."""
    settings = web_redis_settings()
    if not settings.redis_url:
        raise ValueError("web channel transcript store is not configured: set CHANNEL_WEB_REDIS_URL.")
    return settings


def _redis():
    return tai42_app.clients.client_ctx(RedisClient, _redis_settings())


def tail_redis_ctx():
    """A FRESH, non-pooled store connection with the socket read timeout stripped —
    the SSE live tail's blocking XREAD, which a blanket read timeout would kill."""
    return tai42_app.clients.client_ctx(
        RedisClient, _redis_settings().model_copy(update={"socket_timeout": None}), fresh=True
    )


def pooled_redis_ctx():
    """A pooled store connection, for a bounded read that releases it immediately."""
    return _redis()
