"""The kit's Redis connection classes and pools: how a kit connection connects.

Every connection the kit hands out — the async pool's, the :class:`KitSyncConnectionPool`'s,
and the classes :func:`kit_async_connection_class` and :func:`kit_sync_connection_class`
give a pool the kit does not own — makes
its whole connect (socket open, handshake, connect callbacks) one attempt of the
connection's retry, so a connection dropped during its handshake is retried and logged
like a refused one. A command sent on a dropped socket reconnects through ``connect()``
first, so that reconnect is the same one attempt. The connection's ``retry`` is a view of the retry it was given: an
operation retried from inside another one on the same connection runs once, under the
outer operation's budget. A connect callback runs an action whose error must reach the
operation's caller as it is, never retried, with :func:`run_unretried`.

The async connections also open their socket from an empty context, never from the
caller whose command opened them (:class:`EmptyContextConnectionPool`).
"""

import asyncio
import contextvars
import threading
import weakref
from collections.abc import Awaitable, Callable
from typing import Any

from redis.asyncio.connection import (
    AbstractConnection,
    Connection,
    ConnectionPool,
    SSLConnection,
    UnixDomainSocketConnection,
)
from redis.asyncio.retry import Retry as AsyncRetry
from redis.connection import AbstractConnection as SyncAbstractConnection
from redis.connection import Connection as SyncConnection
from redis.connection import ConnectionPool as SyncConnectionPool
from redis.connection import SSLConnection as SyncSSLConnection
from redis.connection import UnixDomainSocketConnection as SyncUnixDomainSocketConnection
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.retry import Retry as SyncRetry

# The error a ``connect`` raises when it is called while the connection's own connect
# attempt runs: that attempt fails, and the connect's retry decides whether another runs.
_DROPPED_DURING_CONNECT = (
    "the Redis connection dropped during its own connect; the connect's retry makes the next attempt"
)


class _UnretriedError(Exception):
    """Carries an action's error through the retries of the operation it was raised in.

    No retry has it among its supported errors, so every retry and every redis-py frame
    between the action and the outermost retried operation lets it pass; that operation's
    view raises :attr:`error` in its place.
    """

    def __init__(self, error: Exception) -> None:
        super().__init__(error)
        self.error = error


class _SyncConnectionRetry(SyncRetry):
    """A sync connection's view of the retry it was given.

    The outermost operation retried on the connection runs under the given retry: its
    budget, its backoff, its WARNINGs. An operation retried from inside that one's ``do``
    (a connect inside a command, the socket open inside a connect) runs its ``do`` once,
    with no loop of its own, so its failure is a failure of the outer operation, which
    makes the next try. An operation retried from a ``fail`` is outermost again and gets
    the full budget. An error raised through :meth:`run_unretried` inside the outermost
    operation is raised to its caller as it is, never retried.

    The view mirrors the given retry's backoff, budget and supported errors, and
    forwards a change of the budget or the supported errors to it.
    """

    __slots__ = ("_depth", "_given")

    def __init__(self, given: SyncRetry) -> None:
        super().__init__(given._backoff, given._retries, given._supported_errors)
        self._given = given
        self._depth = 0

    def update_supported_errors(self, specified_errors: Any) -> None:
        self._given.update_supported_errors(specified_errors)
        self._supported_errors = self._given._supported_errors

    def get_retries(self) -> int:
        return self._retries

    def update_retries(self, value: int) -> None:
        self._given._retries = value
        self._retries = value

    def call_with_retry(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, do: Callable[[], Any], fail: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        if self._depth:
            return do()

        def guarded_do() -> Any:
            self._depth += 1
            try:
                return do()
            finally:
                self._depth -= 1

        try:
            return self._given.call_with_retry(guarded_do, fail, *args, **kwargs)
        except _UnretriedError as carrier:
            error = carrier.error
        # Raised outside the handler, so the error's chain is its own, without the carrier.
        raise error


class _AsyncConnectionRetry(AsyncRetry):
    """An async connection's view of the retry it was given: the async twin of :class:`_SyncConnectionRetry`."""

    __slots__ = ("_depth", "_given")

    def __init__(self, given: AsyncRetry) -> None:
        super().__init__(given._backoff, given._retries, given._supported_errors)
        self._given = given
        self._depth = 0

    def update_supported_errors(self, specified_errors: Any) -> None:
        self._given.update_supported_errors(specified_errors)
        self._supported_errors = self._given._supported_errors

    def get_retries(self) -> int:
        return self._retries

    def update_retries(self, value: int) -> None:
        self._given._retries = value
        self._retries = value

    async def call_with_retry(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, do: Callable[[], Awaitable[Any]], fail: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        if self._depth:
            return await do()

        async def guarded_do() -> Any:
            self._depth += 1
            try:
                return await do()
            finally:
                self._depth -= 1

        try:
            return await self._given.call_with_retry(guarded_do, fail, *args, **kwargs)
        except _UnretriedError as carrier:
            error = carrier.error
        # Raised outside the handler, so the error's chain is its own, without the carrier.
        raise error


def _run_unretried(view: _SyncConnectionRetry | _AsyncConnectionRetry, action: Callable[[], object]) -> None:
    try:
        action()
    except Exception as error:
        if view._depth:
            raise _UnretriedError(error) from None
        raise


class _AsyncConnectsUnderTheRetry(AbstractConnection):
    """An async connection whose whole connect is one attempt of the connection's retry.

    Each attempt is redis-py's ``connect`` — socket open, handshake and connect callbacks
    (a pub/sub re-subscribe) — and the one loop over the attempts is the connection's
    retry: the kit's logged one or any redis-py ``Retry`` it was given. :attr:`retry` is
    the connection's view of that retry (:class:`_AsyncConnectionRetry`), which every
    operation on the connection runs under: an operation retried inside another one —
    the socket open's own retry call inside an attempt, a reconnect inside a command —
    runs once under the outer operation's budget, so every attempt counts once. A failed
    attempt disconnects; it is retried when the error is one of the connection's
    ``retry_on_error``, as on every redis-py command, and raised as it is otherwise or
    past the budget. A ``connect`` called during an attempt (a callback's reconnect after
    a drop) raises a connection error, which fails that attempt. A command sent on a
    dropped socket (:meth:`send_packed_command`) connects through :meth:`connect` first,
    so its reconnect runs under the same retry, handshake included.

    :meth:`run_unretried` runs an action now whose error reaches the caller of the
    outermost retried operation as it is, never retried.
    """

    _in_attempt: bool = False
    _given_retry: AsyncRetry
    _view: _AsyncConnectionRetry

    @property
    def retry(self) -> AsyncRetry:
        """The connection's view of the retry it was given."""
        return self._view

    @retry.setter
    def retry(self, value: AsyncRetry) -> None:
        self._given_retry = value
        self._view = _AsyncConnectionRetry(value)

    def run_unretried(self, action: Callable[[], object]) -> None:
        """Run ``action`` now; an error it raises is never a failure a retry on this connection re-tries.

        Inside a retried operation on the connection the error is raised to that
        operation's caller as it is, the operation's other frames passed untouched;
        outside one it is raised as it is. A ``BaseException`` that is no ``Exception``
        (a cancel) passes untouched.
        """
        _run_unretried(self._view, action)

    async def connect(self) -> None:
        if self.is_connected:
            return
        if self._in_attempt:
            raise RedisConnectionError(_DROPPED_DURING_CONNECT)
        await self.retry.call_with_retry(self._attempt, self._fail)

    async def send_packed_command(self, command: Any, check_health: bool = True) -> None:
        """Send ``command``, connecting under the retry first when the socket is down."""
        await self.connect()
        await super().send_packed_command(command, check_health=check_health)

    async def _attempt(self) -> None:
        self._in_attempt = True
        try:
            await super().connect()
        finally:
            self._in_attempt = False

    async def _fail(self, error: BaseException) -> None:
        await self.disconnect()
        if self.retry_on_error is None or not isinstance(error, tuple(self.retry_on_error)):
            raise error


class _ConnectsFromAnEmptyContext(AbstractConnection):
    """A connection whose socket opens in a task started from an empty context.

    The transport a connect opens keeps the context it was opened in for the
    connection's life, and a pooled connection outlives the caller whose command
    opened it. Every connect of the connection — a pool handing it out, a command
    or a pub/sub reconnecting it, on every redis-py major — opens its socket through
    :meth:`_connect`, so none of them carries a caller's state (its request) into the
    transport. The retry loop and the handshake run in the caller's task, and with
    :class:`_AsyncConnectsUnderTheRetry` the retry spans the handshake.

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


class _TcpConnection(_AsyncConnectsUnderTheRetry, _ConnectsFromAnEmptyContext, Connection):
    pass


class _SslConnection(_AsyncConnectsUnderTheRetry, _ConnectsFromAnEmptyContext, SSLConnection):
    pass


class _UnixConnection(_AsyncConnectsUnderTheRetry, _ConnectsFromAnEmptyContext, UnixDomainSocketConnection):
    pass


# The connection class a Redis URL selects (``redis://``, ``rediss://``, ``unix://``) and
# the one that connects from an empty context, under the connection's retry, in its place.
_EMPTY_CONTEXT_CONNECTIONS: dict[type[AbstractConnection], type[AbstractConnection]] = {
    Connection: _TcpConnection,
    SSLConnection: _SslConnection,
    UnixDomainSocketConnection: _UnixConnection,
}

# The kit async connection class over each base. The classes over redis-py's own three
# connection classes are module-level and live as long as the module; a class over any
# other base lives only as long as something holds it, and its entry goes with it.
_KIT_ASYNC_CONNECTIONS: weakref.WeakValueDictionary[type[AbstractConnection], type[AbstractConnection]] = (
    weakref.WeakValueDictionary(_EMPTY_CONTEXT_CONNECTIONS)
)
_KIT_ASYNC_CONNECTIONS_LOCK = threading.Lock()


def kit_async_connection_class(base: type[AbstractConnection]) -> type[AbstractConnection]:
    """The async connection class over ``base`` that connects as every kit async connection does.

    The async twin of :func:`kit_sync_connection_class`: a subclass of ``base`` whose whole
    connect is one attempt of its retry and whose socket opens from an empty context, the
    same class for the same ``base`` while it is in use (for redis-py's own three classes,
    the one :class:`EmptyContextConnectionPool` gives its connections). A ``base`` that
    already connects under its retry is returned as it is, so a class is never wrapped twice.
    """
    if issubclass(base, _AsyncConnectsUnderTheRetry):
        return base
    with _KIT_ASYNC_CONNECTIONS_LOCK:
        kit_class = _KIT_ASYNC_CONNECTIONS.get(base)
        if kit_class is None:
            kit_class = type(
                base.__name__,
                (_AsyncConnectsUnderTheRetry, _ConnectsFromAnEmptyContext, base),
                {"__module__": __name__},
            )
            _KIT_ASYNC_CONNECTIONS[base] = kit_class
        return kit_class


class EmptyContextConnectionPool(ConnectionPool):
    """A connection pool whose connections connect from an empty context, never their caller's.

    Each connect of its connections is one attempt of the connection's retry, handshake included.
    """

    def __init__(self, connection_class: type[AbstractConnection] = Connection, **connection_kwargs: Any) -> None:
        """redis-py's pool over the empty-context class of ``connection_class``; any other class raises."""
        empty_context_class = _EMPTY_CONTEXT_CONNECTIONS.get(connection_class)
        if empty_context_class is None:
            raise TypeError(
                f"no empty-context connect is defined for the Redis connection class {connection_class.__qualname__}; "
                f"supported: {sorted(cls.__qualname__ for cls in _EMPTY_CONTEXT_CONNECTIONS)}"
            )
        super().__init__(connection_class=empty_context_class, **connection_kwargs)


class _SyncConnectsUnderTheRetry(SyncAbstractConnection):
    """A sync connection whose whole connect is one attempt of the connection's retry.

    The sync twin of :class:`_AsyncConnectsUnderTheRetry`: one loop over attempts of
    redis-py's ``connect`` (socket, handshake, connect callbacks) under the connection's
    retry, :attr:`retry` the connection's view of it (:class:`_SyncConnectionRetry`), the
    ``retry_on_error`` policy on a failed attempt, a ``connect`` during an attempt
    raising, a command sent on a dropped socket (:meth:`send_packed_command`) connecting
    through :meth:`connect` first, and :meth:`run_unretried`.
    """

    _in_attempt: bool = False
    _given_retry: SyncRetry
    _view: _SyncConnectionRetry
    # redis-py's sync connection stores the errors it retries on as a list.
    retry_on_error: list[type[Exception]]

    @property
    def retry(self) -> SyncRetry:
        """The connection's view of the retry it was given."""
        return self._view

    @retry.setter
    def retry(self, value: SyncRetry) -> None:
        self._given_retry = value
        self._view = _SyncConnectionRetry(value)

    def run_unretried(self, action: Callable[[], object]) -> None:
        """Run ``action`` now; an error it raises is never a failure a retry on this connection re-tries.

        As :meth:`_AsyncConnectsUnderTheRetry.run_unretried`.
        """
        _run_unretried(self._view, action)

    def connect(self) -> None:
        if self._sock:
            return
        if self._in_attempt:
            raise RedisConnectionError(_DROPPED_DURING_CONNECT)
        self.retry.call_with_retry(self._attempt, self._fail)

    def send_packed_command(self, command: Any, check_health: bool = True) -> None:
        """Send ``command``, connecting under the retry first when the socket is down."""
        self.connect()
        super().send_packed_command(command, check_health=check_health)

    def _attempt(self) -> None:
        self._in_attempt = True
        try:
            super().connect()
        finally:
            self._in_attempt = False

    def _fail(self, error: BaseException) -> None:
        self.disconnect(error)
        if self.retry_on_error is None or not isinstance(error, tuple(self.retry_on_error)):
            raise error


class _SyncTcpConnection(_SyncConnectsUnderTheRetry, SyncConnection):
    pass


class _SyncSslConnection(_SyncConnectsUnderTheRetry, SyncSSLConnection):
    pass


class _SyncUnixConnection(_SyncConnectsUnderTheRetry, SyncUnixDomainSocketConnection):
    pass


# The kit sync connection class over each base. The classes over redis-py's own three
# connection classes are module-level and live as long as the module; a class over any
# other base (a connection class a pool's owner defines per pool) lives only as long as
# something holds it, and its entry goes with it.
_KIT_SYNC_CONNECTIONS: weakref.WeakValueDictionary[type[SyncAbstractConnection], type[SyncAbstractConnection]] = (
    weakref.WeakValueDictionary(
        {
            SyncConnection: _SyncTcpConnection,
            SyncSSLConnection: _SyncSslConnection,
            SyncUnixDomainSocketConnection: _SyncUnixConnection,
        }
    )
)
_KIT_SYNC_CONNECTIONS_LOCK = threading.Lock()


def kit_sync_connection_class(base: type[SyncAbstractConnection]) -> type[SyncAbstractConnection]:
    """The sync connection class over ``base`` whose whole connect is one attempt of its retry.

    A subclass of ``base`` (and so of every class ``base`` derives from), the same class
    for the same ``base`` while it is in use. A ``base`` that already connects under its
    retry is returned as it is, so a class is never wrapped twice.
    """
    if issubclass(base, _SyncConnectsUnderTheRetry):
        return base
    with _KIT_SYNC_CONNECTIONS_LOCK:
        kit_class = _KIT_SYNC_CONNECTIONS.get(base)
        if kit_class is None:
            kit_class = type(base.__name__, (_SyncConnectsUnderTheRetry, base), {"__module__": __name__})
            _KIT_SYNC_CONNECTIONS[base] = kit_class
        return kit_class


class KitSyncConnectionPool(SyncConnectionPool):
    """A sync connection pool whose connections are the kit sync class over the class it is given.

    ``ConnectionPool.from_url`` resolves the URL's scheme to its connection class (or
    takes a ``connection_class`` kwarg) before building the pool, so every scheme's
    connection connects under its retry, handshake included.
    """

    def __init__(
        self, connection_class: type[SyncAbstractConnection] = SyncConnection, **connection_kwargs: Any
    ) -> None:
        """redis-py's pool over :func:`kit_sync_connection_class` of ``connection_class``."""
        # redis-py leaves ``connection_class`` unannotated, so its type is read off the
        # ``Connection`` default; the pool takes any connection class (its ``from_url``
        # gives it ``UnixDomainSocketConnection``, which is no ``Connection``).
        super().__init__(
            connection_class=kit_sync_connection_class(connection_class),  # pyright: ignore[reportArgumentType]
            **connection_kwargs,
        )


def run_unretried(connection: object, action: Callable[[], object]) -> None:
    """Run ``action`` now on ``connection``; an error it raises is never retried on it.

    ``connection`` is a kit connection, sync or async (one of every kit pool's), and the
    action runs as its :meth:`~_SyncConnectsUnderTheRetry.run_unretried` says; any other
    connection raises ``TypeError`` naming its class and the missing ``run_unretried``.
    """
    if not isinstance(connection, (_AsyncConnectsUnderTheRetry, _SyncConnectsUnderTheRetry)):
        raise TypeError(
            f"{type(connection).__qualname__} is no kit Redis connection: it has no run_unretried "
            "to run an action shielded from its retry"
        )
    connection.run_unretried(action)
