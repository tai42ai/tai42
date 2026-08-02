import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Mapping
from typing import Any, cast

from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis

from tai42_kit.clients.base import PooledClient, reject_unknown_connection_kwargs
from tai42_kit.settings import not_configured_message

# Connection kwargs a Redis client accepts — the JSON-serializable identity
# produced by ``RedisConnectionSettings.client_kwargs``. ``env_prefix`` rides
# along only to name the missing env var when the URL is unset; it is dropped
# before ``from_url`` and excluded from the pool key. Anything else is a typo or
# a stray value and is rejected so it can't silently split the pool key.
_ALLOWED_KWARGS = frozenset(
    {
        "url",
        "max_connections",
        "decode_responses",
        "socket_timeout",
        "socket_connect_timeout",
        "retry_on_timeout",
        "retry_attempts",
        "env_prefix",
    }
)


def _pool_key(**kwargs: Any) -> str:
    """The pool key for a Redis client — connection identity only.

    ``env_prefix`` travels in the connection kwargs so an unresolved URL can name
    the env var that would set it, but it is not connection identity; dropping it
    here keeps two settings classes that resolve to the same URL on one shared
    pool rather than splitting it per namespace."""
    identity = {key: value for key, value in kwargs.items() if key != "env_prefix"}
    return json.dumps(identity, sort_keys=True)


def _validate_kwargs(kwargs: dict[str, Any]) -> None:
    _validate_url(kwargs)
    reject_unknown_connection_kwargs("Redis client", kwargs, _ALLOWED_KWARGS)


def _validate_url(kwargs: dict[str, Any]) -> None:
    if "url" not in kwargs:
        raise KeyError("Redis client requires a 'url' kwarg")
    url = kwargs["url"]
    if not url:
        env_prefix = kwargs.get("env_prefix") or ""
        raise ValueError(
            not_configured_message("the Redis connection", f"{env_prefix}REDIS_URL", "TAI_DEFAULT_REDIS_URL")
        )
    if not isinstance(url, str):
        raise TypeError("Redis client 'url' must be a string (e.g., 'redis://localhost:6379/0')")


class RedisClient(PooledClient[AsyncRedis]):
    @staticmethod
    def _key(**kwargs: Any) -> str:
        return _pool_key(**kwargs)

    async def _create(self, **kwargs) -> AsyncRedis:
        _validate_kwargs(kwargs)
        return await AsyncRedis.from_url(**_with_retry(kwargs)).initialize()

    async def _close(self, client: AsyncRedis):
        await client.aclose()

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        return RedisConnectionError, RedisTimeoutError


class SyncRedisClient(PooledClient[SyncRedis]):
    @staticmethod
    def _key(**kwargs: Any) -> str:
        return _pool_key(**kwargs)

    async def _create(self, **kwargs) -> SyncRedis:
        _validate_kwargs(kwargs)
        return SyncRedis.from_url(**_with_retry(kwargs, sync=True))

    async def _close(self, client: SyncRedis):
        # Sync close can do network I/O to release pool connections — keep it
        # off the event loop.
        await asyncio.to_thread(client.close)

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        return RedisConnectionError, RedisTimeoutError


def _with_retry(kwargs: dict[str, Any], *, sync: bool = False) -> dict[str, Any]:
    """Turn the serializable ``retry_attempts`` int into a redis ``Retry`` object.

    ``retry_attempts`` is part of the pool key (a stable int) but is not a valid
    ``Redis.from_url`` argument, so it is consumed here and, when positive,
    materialized into an exponential-backoff ``Retry`` on connection/timeout
    errors — built per client because a ``Retry`` instance is not serializable.
    The sync and async clients import ``Retry`` from their own subpackage.
    """
    # ``env_prefix`` is a message-only passenger, not a ``from_url`` argument.
    kwargs.pop("env_prefix", None)
    retry_attempts = kwargs.pop("retry_attempts", 0)
    if retry_attempts > 0:
        if sync:
            from redis.retry import Retry
        else:
            from redis.asyncio.retry import Retry
        from redis.backoff import ExponentialBackoff
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        kwargs["retry"] = Retry(ExponentialBackoff(), retry_attempts)
        kwargs["retry_on_error"] = [RedisConnectionError, RedisTimeoutError]
    return kwargs


# Typed seams over redis-py 7.x async client stubs.
#
# redis-py 7.x annotates command methods with the shared sync/async signature
# (``Awaitable[T] | T``), so ``await client.hgetall(...)`` fails type checking
# even though the async client always returns an awaitable at runtime. These
# helpers pin the async half of the hash commands, confining the required casts
# to one place; they add no runtime behavior beyond a plain function call.


def hgetall(client: AsyncRedis, key: str) -> Awaitable[dict[str, str]]:
    """``client.hgetall(key)`` pinned to the async client's true awaitable return
    (decoded-responses shape: a field/value string map, ``{}`` for a missing key)."""
    return cast("Awaitable[dict[str, str]]", client.hgetall(key))


def hset_mapping(client: AsyncRedis, key: str, mapping: Mapping[str, str]) -> Awaitable[int]:
    """``client.hset(key, mapping=mapping)`` pinned to the async client's true
    awaitable return (the count of newly added fields)."""
    return cast("Awaitable[int]", client.hset(key, mapping=dict(mapping)))


def scan_iter(client: AsyncRedis, match: str) -> AsyncIterator[Any]:
    """``client.scan_iter(match=match)`` pinned to the async client's iterator."""
    return cast("AsyncIterator[Any]", client.scan_iter(match=match))
