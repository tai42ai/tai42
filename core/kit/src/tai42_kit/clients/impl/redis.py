"""Pooled Redis clients (async and sync) and typed seams over the redis-py async hash commands.

Identity model: one pooled client per full connection configuration per event loop —
the URL, the decode mode, ``max_connections``, the socket timeouts and the retry
options together. For redis-py these connection options ARE the client's contract: a
blocking stream reader and a command client on one URL are different clients, so a
configuration that differs in any of these values gets its own pooled client by
design rather than being forced to share one. ``env_prefix`` is a message-only
passenger — it names the owning component's env var when the URL is unset and never
splits a pool (the base pool keeps it out of the identity). A Redis client carries no
build options, so the pool's build-option conflict guard never fires for it.

Every Redis client the kit builds takes its ``from_url`` kwargs from
:func:`redis_from_url_kwargs` (or, for a door that brings its own connection string, its
behaviour half from :func:`redis_connection_options`): the one place a retry is built.
The retry is the kit's logged subclass of redis-py's ``Retry``, so every retried failure
logs a WARNING named by a setting, never by the URL. Every sync client that can carry
that retry is a :class:`KitSyncRedis`, whose pub/sub is a :class:`ReentrantPubSub`.

Every async client the kit builds comes from :func:`async_redis_from_url`, whose
connections connect from an empty context, never from the caller whose command opened them.
On the asyncio selector loop (not uvloop), a reply larger than the stream reader's pause
threshold re-registers the connection's reader in the context of the request reading it,
which then stays held until the next such reply on that connection or the connection's close.
"""

import asyncio
import contextvars
import logging
import threading
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Self, TypeVar, cast
from urllib.parse import urlsplit

from redis import Redis as SyncRedis
from redis.asyncio import Redis as AsyncRedis
from redis.asyncio.client import PubSub as AsyncPubSub
from redis.asyncio.connection import (
    AbstractConnection,
    Connection,
    ConnectionPool,
    SSLConnection,
    UnixDomainSocketConnection,
)
from redis.asyncio.retry import Retry as AsyncRetry
from redis.backoff import AbstractBackoff, ExponentialBackoff, NoBackoff
from redis.client import PubSub as SyncPubSub
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from redis.retry import Retry as SyncRetry

from tai42_kit.clients.backoff import CONNECT_RETRY_BACKOFF_BASE_SECONDS, CONNECT_RETRY_BACKOFF_CAP_SECONDS
from tai42_kit.clients.base import PooledClient, reject_unknown_connection_kwargs
from tai42_kit.settings import not_configured_message

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

# Connection kwargs a Redis client accepts — the JSON-serializable kwargs produced by
# ``RedisConnectionSettings.client_kwargs``. Every one but ``env_prefix`` is part of the
# pool identity: the URL, the decode mode, ``max_connections``, the socket timeouts and
# the retry options together select the pooled client. ``env_prefix`` rides along only to
# name the missing env var when the URL is unset (a passenger the base pool keeps out of
# the identity; dropped before ``from_url``). Anything else is a typo or a stray value and
# is rejected so it can't silently change the connection.
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

# The behaviour kwargs :func:`redis_connection_options` turns into redis-py kwargs: the
# socket timeouts and the retry options, with no connection identity.
_BEHAVIOUR_KWARGS = frozenset({"socket_timeout", "socket_connect_timeout", "retry_on_timeout", "retry_attempts"})

# The identity kwargs :func:`redis_from_url_kwargs` passes to ``from_url`` as they are.
_IDENTITY_KWARGS = ("url", "max_connections", "decode_responses")


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


class _ConnectsFromAnEmptyContext(AbstractConnection):
    """A connection whose socket opens in a task started from an empty context.

    The transport a connect opens keeps the context it was opened in for the
    connection's life, and a pooled connection outlives the caller whose command
    opened it. Every connect of the connection — a pool handing it out, a command
    or a pub/sub reconnecting it, on every redis-py major — opens its socket through
    :meth:`_connect`, so none of them carries a caller's state (its request) into the
    transport. The retry loop and the handshake run in the caller's task.

    A caller cancelled after the socket opened and before its handshake ran closes the
    socket before the cancel propagates, so the pool never hands out a connection that
    was never set up.
    """

    async def _connect(self) -> None:
        opening = asyncio.create_task(super()._connect(), context=contextvars.Context())
        try:
            await opening
        except asyncio.CancelledError:
            if self.is_connected:
                await self.disconnect(nowait=True)
            raise


class _TcpConnection(_ConnectsFromAnEmptyContext, Connection):
    pass


class _SslConnection(_ConnectsFromAnEmptyContext, SSLConnection):
    pass


class _UnixConnection(_ConnectsFromAnEmptyContext, UnixDomainSocketConnection):
    pass


# The connection class a Redis URL selects (``redis://``, ``rediss://``, ``unix://``) and
# the one that connects from an empty context in its place.
_EMPTY_CONTEXT_CONNECTIONS: dict[type[AbstractConnection], type[AbstractConnection]] = {
    Connection: _TcpConnection,
    SSLConnection: _SslConnection,
    UnixDomainSocketConnection: _UnixConnection,
}


class _EmptyContextConnectionPool(ConnectionPool):
    """A connection pool whose connections connect from an empty context, never their caller's."""

    def __init__(self, connection_class: type[AbstractConnection] = Connection, **connection_kwargs: Any) -> None:
        empty_context_class = _EMPTY_CONTEXT_CONNECTIONS.get(connection_class)
        if empty_context_class is None:
            raise TypeError(
                f"no empty-context connect is defined for the Redis connection class {connection_class.__qualname__}; "
                f"supported: {sorted(cls.__qualname__ for cls in _EMPTY_CONTEXT_CONNECTIONS)}"
            )
        super().__init__(connection_class=empty_context_class, **connection_kwargs)


class KitAsyncPubSub(AsyncPubSub):
    """redis-py's async ``PubSub`` that runs ``on_reconnect`` after each re-subscribe.

    The connection's retry reconnects a dropped pub/sub inside the read or command that
    met the drop and re-subscribes every channel there, so no error reaches the
    subscriber, and a message published while it was disconnected was delivered to no
    one. ``on_reconnect`` runs once that re-subscribe is done, so the subscriber can read
    back what it missed. The first connect does not run it.
    """

    def __init__(self, *args: Any, on_reconnect: Callable[[], None] | None = None, **kwargs: Any) -> None:
        """Build redis-py's async ``PubSub`` from ``args`` / ``kwargs``; ``on_reconnect`` runs on each re-subscribe."""
        super().__init__(*args, **kwargs)
        self._on_reconnect = on_reconnect

    async def on_connect(self, connection: Any) -> None:
        """Re-subscribe every channel on the reconnected ``connection``, then run ``on_reconnect``."""
        await super().on_connect(connection)
        if self._on_reconnect is not None:
            self._on_reconnect()


class KitAsyncRedis(AsyncRedis):
    """redis-py's async client whose :meth:`pubsub` is a :class:`KitAsyncPubSub`.

    Every async client the kit builds is one.
    """

    @classmethod
    def from_pool(cls, connection_pool: ConnectionPool) -> Self:
        """redis-py's ``from_pool``, which builds ``cls``, typed as building it."""
        return cast("Self", super().from_pool(connection_pool))

    def pubsub(self, *, on_reconnect: Callable[[], None] | None = None, **kwargs: Any) -> KitAsyncPubSub:
        """A :class:`KitAsyncPubSub` on this client's pool; ``kwargs`` as redis-py's ``pubsub``."""
        return KitAsyncPubSub(
            self.connection_pool, on_reconnect=on_reconnect, event_dispatcher=self._event_dispatcher, **kwargs
        )


def async_redis_from_url(url: str, **kwargs: Any) -> KitAsyncRedis:
    """An async Redis client for ``url`` whose pooled connections connect from an empty context.

    The same client ``Redis.from_url(url, **kwargs)`` builds, owning its pool, over a
    :class:`_EmptyContextConnectionPool`, as a :class:`KitAsyncRedis`.
    """
    return KitAsyncRedis.from_pool(_EmptyContextConnectionPool.from_url(url, **kwargs))


class RedisClient(PooledClient[KitAsyncRedis]):
    """A pooled async Redis client keyed on its full connection configuration.

    One client per configuration per event loop — URL, decode mode, ``max_connections``,
    socket timeouts and retry options; ``env_prefix`` is a passenger that never splits a
    pool.
    """

    async def _create(self, **kwargs) -> KitAsyncRedis:
        _validate_kwargs(kwargs)
        return await async_redis_from_url(**redis_from_url_kwargs(kwargs)).initialize()

    async def _close(self, client: KitAsyncRedis):
        await client.aclose()

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        return RedisConnectionError, RedisTimeoutError


class ReentrantPubSub(SyncPubSub):
    """redis-py's sync ``PubSub`` with a re-entrant command lock and a reconnect hook.

    A pub/sub command runs under the pubsub's lock. When its send fails, the connection's
    retry reconnects, and the connect callback re-subscribes every channel from the same
    thread while the lock is still held; on redis-py 5 that lock is not re-entrant, so the
    re-subscribe blocks forever. With a re-entrant lock the re-subscribe runs, then the
    failed command is sent again, as redis-py 6 and later do.

    ``on_reconnect`` runs after each re-subscribe, as on :class:`KitAsyncPubSub`.
    """

    def __init__(self, *args: Any, on_reconnect: Callable[[], None] | None = None, **kwargs: Any) -> None:
        """Build redis-py's ``PubSub`` with ``args`` / ``kwargs``, on a re-entrant lock."""
        super().__init__(*args, **kwargs)
        self._lock = threading.RLock()
        self._on_reconnect = on_reconnect

    def on_connect(self, connection: Any) -> None:
        """Re-subscribe every channel on the reconnected ``connection``, then run ``on_reconnect``."""
        super().on_connect(connection)
        if self._on_reconnect is not None:
            self._on_reconnect()


class KitSyncRedis(SyncRedis):
    """redis-py's sync client whose :meth:`pubsub` is a :class:`ReentrantPubSub`.

    Every sync client the kit builds is one, and so is the client of every sync door that
    takes the kit's connection options, so a pub/sub on the kit's retry reconnects.
    """

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> Self:
        """redis-py's ``from_url``, which builds ``cls``, typed as building it."""
        return cast("Self", super().from_url(url, **kwargs))

    def pubsub(self, *, on_reconnect: Callable[[], None] | None = None, **kwargs: Any) -> ReentrantPubSub:
        """A :class:`ReentrantPubSub` on this client's pool; ``kwargs`` as redis-py's ``pubsub``."""
        return ReentrantPubSub(
            self.connection_pool, on_reconnect=on_reconnect, event_dispatcher=self._event_dispatcher, **kwargs
        )


class SyncRedisClient(PooledClient[KitSyncRedis]):
    """A pooled sync Redis client keyed on its full connection configuration.

    One client per configuration per event loop — URL, decode mode, ``max_connections``,
    socket timeouts and retry options; ``env_prefix`` is a passenger that never splits a
    pool.
    """

    async def _create(self, **kwargs) -> KitSyncRedis:
        _validate_kwargs(kwargs)
        return KitSyncRedis.from_url(**redis_from_url_kwargs(kwargs, sync=True))

    async def _close(self, client: KitSyncRedis):
        # Sync close can do network I/O to release pool connections — keep it
        # off the event loop.
        await asyncio.to_thread(client.close)

    def _disconnection_exceptions(self) -> tuple[type[Exception], ...]:
        from redis.exceptions import ConnectionError as RedisConnectionError
        from redis.exceptions import TimeoutError as RedisTimeoutError

        return RedisConnectionError, RedisTimeoutError


class _LoggedAsyncRetry(AsyncRetry):
    """redis-py's async ``Retry`` that logs a WARNING for every failure it retries.

    One instance serves the connect, the commands and the pub/sub reads of its
    connection. A call made from inside this retry's own ``do`` (a command reconnecting
    its connection) runs once and the outer call owns the budget; a call made from
    ``fail`` (a pub/sub read reconnecting) is a connect of its own with the full budget.
    The caller's extra arguments and the arguments the loop gives ``fail`` are passed
    on unchanged, whatever the redis-py major.
    """

    __slots__ = ("_budget", "_label", "_running")

    def __init__(self, backoff: AbstractBackoff, retries: int, *, label: str) -> None:
        super().__init__(backoff, retries)
        self._label = label
        self._budget = retries
        self._running = False

    async def call_with_retry(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, do: Callable[[], Awaitable[_T]], fail: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> _T:
        if self._running:
            return await do()
        failures = 0

        async def guarded_do() -> _T:
            self._running = True
            try:
                return await do()
            finally:
                self._running = False

        async def logged_fail(error: BaseException, *rest: Any) -> None:
            nonlocal failures
            failures += 1
            await fail(error, *rest)
            if failures <= self._budget:
                _log_retry(self._label, failures, self._budget, error, self._backoff.compute(failures))

        return await super().call_with_retry(guarded_do, logged_fail, *args, **kwargs)


class _LoggedSyncRetry(SyncRetry):
    """redis-py's sync ``Retry`` that logs a WARNING for every failure it retries.

    The same guard and pass-through as :class:`_LoggedAsyncRetry`, with plain calls.
    """

    __slots__ = ("_budget", "_label", "_running")

    def __init__(self, backoff: AbstractBackoff, retries: int, *, label: str) -> None:
        super().__init__(backoff, retries)
        self._label = label
        self._budget = retries
        self._running = False

    def call_with_retry(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, do: Callable[[], _T], fail: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> _T:
        if self._running:
            return do()
        failures = 0

        def guarded_do() -> _T:
            self._running = True
            try:
                return do()
            finally:
                self._running = False

        def logged_fail(error: BaseException, *rest: Any) -> None:
            nonlocal failures
            failures += 1
            fail(error, *rest)
            if failures <= self._budget:
                _log_retry(self._label, failures, self._budget, error, self._backoff.compute(failures))

        return super().call_with_retry(guarded_do, logged_fail, *args, **kwargs)


def _log_retry(label: str, failures: int, budget: int, error: BaseException, delay: float) -> None:
    logger.warning(
        "Redis connection %s failed (attempt %d of %d): %s; retrying in %.3fs",
        label,
        failures,
        budget + 1,
        error,
        delay,
    )


def redis_connection_options(options: Mapping[str, Any], *, sync: bool = False, label: str) -> dict[str, Any]:
    """The redis-py connection behaviour kwargs (retry, retry_on_error, timeouts) for ``options``.

    ``options`` carries only the behaviour keys (``socket_timeout``,
    ``socket_connect_timeout``, ``retry_on_timeout``, ``retry_attempts``); any other key
    raises. No URL is read or validated, so a door that brings its own connection string
    uses it as it is. ``label`` names the connection in the retry's WARNINGs.

    - ``retry_attempts > 0``: the kit's logged retry with that many retries and the kit's
      exponential backoff, retrying redis-py's ``ConnectionError`` / ``TimeoutError`` and a
      raw ``OSError`` from the socket connect.
    - ``retry_attempts == 0`` with ``retry_on_timeout``: the kit's logged retry with one
      retry and no backoff — the retry redis-py attaches itself in that case.
    - Otherwise no retry: redis-py's own single attempt.

    ``retry_attempts`` never reaches redis-py; a ``Retry`` is built per client because it
    is not serializable.
    """
    reject_unknown_connection_kwargs("Redis connection options", dict(options), _BEHAVIOUR_KWARGS)
    built: dict[str, Any] = {}
    for key in ("socket_timeout", "socket_connect_timeout"):
        if options.get(key) is not None:
            built[key] = options[key]
    retry_on_timeout = bool(options.get("retry_on_timeout", False))
    if retry_on_timeout:
        built["retry_on_timeout"] = True
    retry_cls = _LoggedSyncRetry if sync else _LoggedAsyncRetry
    retry_attempts = options.get("retry_attempts", 0)
    if retry_attempts > 0:
        backoff = ExponentialBackoff(cap=CONNECT_RETRY_BACKOFF_CAP_SECONDS, base=CONNECT_RETRY_BACKOFF_BASE_SECONDS)
        built["retry"] = retry_cls(backoff, retry_attempts, label=label)
        built["retry_on_error"] = [RedisConnectionError, RedisTimeoutError, OSError]
    elif retry_on_timeout:
        built["retry"] = retry_cls(NoBackoff(), 1, label=label)
    return built


def redis_from_url_kwargs(kwargs: Mapping[str, Any], *, sync: bool = False, label: str | None = None) -> dict[str, Any]:
    """The ``Redis.from_url`` kwargs for the kit's client kwargs, ``url`` included.

    Validates ``kwargs`` (a set URL, only the kit's connection kwargs) without changing
    the caller's mapping, drops the ``env_prefix`` passenger, keeps the identity kwargs
    and adds the behaviour kwargs of :func:`redis_connection_options`. The retry's
    WARNINGs name the connection by ``label`` when given, else by the store's URL
    setting (``<env_prefix>REDIS_URL``) when the kwargs carry ``env_prefix``, else by the
    URL's host, port and database with no credentials.
    """
    source = dict(kwargs)
    _validate_kwargs(source)
    built = {key: source[key] for key in _IDENTITY_KWARGS if key in source}
    behaviour = {key: source[key] for key in _BEHAVIOUR_KWARGS if key in source}
    if label is None:
        label = f"{source['env_prefix']}REDIS_URL" if "env_prefix" in source else _url_label(source["url"])
    built.update(redis_connection_options(behaviour, sync=sync, label=label))
    return built


def _url_label(url: str) -> str:
    """The URL's ``host:port/db`` (or a unix socket's path), never its credentials."""
    parts = urlsplit(url)
    if parts.scheme == "unix":
        return parts.path
    host = parts.hostname or ""
    port = f":{parts.port}" if parts.port is not None else ""
    return f"{host}{port}{parts.path}"


# Typed seams over redis-py 7.x async client stubs.
#
# redis-py 7.x annotates command methods with the shared sync/async signature
# (``Awaitable[T] | T``), so ``await client.hgetall(...)`` fails type checking
# even though the async client always returns an awaitable at runtime. These
# helpers pin the async half of the hash commands, confining the required casts
# to one place; they add no runtime behavior beyond a plain function call.


def hgetall(client: AsyncRedis, key: str) -> Awaitable[dict[str, str]]:
    """``client.hgetall(key)`` pinned to the async client's true awaitable return.

    Decoded-responses shape: a field/value string map, ``{}`` for a missing key.
    """
    return cast("Awaitable[dict[str, str]]", client.hgetall(key))


def hset_mapping(client: AsyncRedis, key: str, mapping: Mapping[str, str]) -> Awaitable[int]:
    """``client.hset(key, mapping=mapping)`` pinned to the async client's true awaitable return.

    The count of newly added fields.
    """
    return cast("Awaitable[int]", client.hset(key, mapping=dict(mapping)))


def scan_iter(client: AsyncRedis, match: str) -> AsyncIterator[Any]:
    """``client.scan_iter(match=match)`` pinned to the async client's iterator."""
    return cast("AsyncIterator[Any]", client.scan_iter(match=match))
