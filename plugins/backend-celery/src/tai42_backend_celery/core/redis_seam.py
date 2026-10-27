"""The kit's Redis connection behaviour on the pools celery and kombu build themselves.

celery's result backend and kombu's broker transport parse their own URLs and build
their own redis-py pools, so the kit seam reaches them through subclasses: each keeps the
library-parsed identity (host, port, db or virtual host, credentials, TLS, unix socket
path, kombu's key prefix and its poller-aware connection class) and takes the connection
behaviour (retry, ``retry_on_error``, ``retry_on_timeout``, the socket timeouts) from
:func:`~tai42_kit.clients.impl.redis.redis_connection_options`. A knob left unset keeps
the library's own default. Every connection either side builds is the kit's sync
connection over the library's class, so its whole connect — socket, handshake and
connect callbacks — is one attempt of that retry.

- :class:`KitRedisBackend`, the Redis result backend, with ``CELERY_RESULT_BACKEND_*``.
  :class:`KitLoader` selects it for every ``redis`` / ``rediss`` backend URL. Its result
  consumer (:class:`KitResultConsumer`) reads the stored state of every awaited task
  after each pub/sub reconnect the retry makes, so a result written meanwhile is not lost.
- :class:`KitRedisTransport` (channel :class:`KitRedisChannel`), the Redis broker
  transport, with ``CELERY_BROKER_*`` for the read and the write side alike.
  :class:`KitAMQP` makes every broker connection a :class:`KitConnection`, which selects
  it whenever kombu resolves its own Redis transport. The connections kombu polls (a
  worker's BRPOP and fanout LISTEN, a control command's reply read in any process) leave
  the retry once their channel has registered with kombu's poller, at channel open: a
  drop there raises. A worker's consumer restart reconnects them; a control call fails
  and a retry of that call reconnects.

Every sync client either side builds is a :class:`~tai42_kit.clients.impl.redis.KitSyncRedis`
(or its key-prefixed form), whose pub/sub reconnects under the retry its connection carries.

Both hooks act on the URL celery resolves, whichever env var or conf key named it, so the
URL is never rewritten. :func:`configure_redis_seam` checks, at app creation, that every
Redis URL in effect is a form the seam carries, and turns off celery's startup connect
retry and publish retry on a side the seam covers.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import os
from collections.abc import Callable, Iterator, Mapping
from typing import Any, cast

import kombu
from celery import Celery
from celery.app.amqp import AMQP
from celery.app.backends import by_url
from celery.backends.redis import RedisBackend, ResultConsumer
from celery.exceptions import ImproperlyConfigured
from celery.loaders.app import AppLoader
from kombu.connection import ConnectionPool, PooledConnection
from kombu.transport.redis import Channel as RedisChannel
from kombu.transport.redis import PrefixedRedisPubSub, PrefixedStrictRedis
from kombu.transport.redis import Transport as RedisTransport
from redis.backoff import NoBackoff
from redis.retry import Retry
from tai42_kit.clients.impl.redis import KitSyncRedis, ReentrantPubSub, redis_connection_options
from tai42_kit.clients.impl.redis_connections import KitSyncConnectionPool, kit_sync_connection_class

from tai42_backend_celery.core.settings import celery_settings

# The import path celery's backend aliases resolve for a Redis result backend URL.
KIT_REDIS_BACKEND = "tai42_backend_celery.core.redis_seam:KitRedisBackend"

# The env name the result backend's retry WARNINGs carry: the URL setting celery reads
# (``CELERY_RESULT_BACKEND``), which is also this plugin's own result backend setting.
RESULT_BACKEND_LABEL = "CELERY_RESULT_BACKEND"

# The broker URL settings: celery reads the two side vars before the shared one.
BROKER_URL_SETTING = "CELERY_BROKER_URL"
BROKER_READ_URL_SETTING = "CELERY_BROKER_READ_URL"
BROKER_WRITE_URL_SETTING = "CELERY_BROKER_WRITE_URL"

REDBEAT_URL_SETTING = "CELERY_REDBEAT_REDIS_URL"

# The URL schemes RedBeat opens with ``Redis.from_url``, the client the seam's options reach.
_REDBEAT_SEAM_SCHEMES = frozenset({"redis", "rediss", "unix"})

# Unix socket URL forms neither celery nor kombu has a class for; both take ``redis+socket://``.
_UNOPENABLE_SOCKET_SCHEMES = frozenset({"unix", "socket"})

_SEAM_FORMS = "use one redis://, rediss:// or redis+socket:// URL"


class UnsupportedRedisUrlError(ValueError):
    """A Redis URL in a form the kit's Redis connection cannot carry, raised at app creation.

    The message names the setting and the URL scheme, never the URL, which can carry a
    password.
    """

    def __init__(self, setting: str, scheme: str, hint: str) -> None:
        """Name the ``setting`` in effect, its URL ``scheme`` and a ``hint`` on the forms that work."""
        super().__init__(f"{setting}: a {scheme}:// URL is not supported for this Redis connection; {hint}")
        self.setting = setting
        self.scheme = scheme


def _scheme(url: str) -> str:
    return url.partition("://")[0]


# --- result backend ------------------------------------------------------------


class _ResultClient(KitSyncRedis):
    """The result backend's client, whose pub/subs run ``on_pubsub_reconnect`` after each re-subscribe."""

    def __init__(self, *args: Any, on_pubsub_reconnect: Callable[[], None], **kwargs: Any) -> None:
        """Build the client with ``args`` / ``kwargs``; its pub/subs run ``on_pubsub_reconnect``."""
        super().__init__(*args, **kwargs)
        self._on_pubsub_reconnect = on_pubsub_reconnect

    def pubsub(self, **kwargs: Any) -> ReentrantPubSub:
        """A kit :class:`~tai42_kit.clients.impl.redis.ReentrantPubSub` that runs ``on_pubsub_reconnect``."""
        return super().pubsub(on_reconnect=self._on_pubsub_reconnect, **kwargs)


class KitResultConsumer(ResultConsumer):
    """celery's Redis result consumer on the kit seam's retry.

    The pool's retry reconnects a dropped pub/sub and re-subscribes it inside the read or
    command that met the drop, so celery's own reconnect does not run for it; the pub/sub
    then calls :meth:`catch_up`, which reads what celery's reconnect reads. When the retry
    gives up, the wait raises the connection error, and the next wait (on a task already
    awaited or a new one) builds a new pub/sub with every awaited task subscribed, then
    catches up.
    """

    def catch_up(self) -> None:
        """Deliver the stored state of every task still awaited, read after a re-subscribe.

        A result written while the pub/sub was disconnected was published to no one; its
        stored state is the only copy left.
        """
        if self.subscribed_to:
            for meta in self.backend.client.mget(self.subscribed_to):
                if meta:
                    self.on_state_change(self._decode_result(meta), None)

    @contextlib.contextmanager
    def reconnect_on_error(self) -> Iterator[None]:
        """Rebuild the pub/sub after a connection error the retry gave up on.

        A reconnect that fails raises its own connection error: the retry behind it has
        made every attempt ``CELERY_RESULT_BACKEND_RETRY_ATTEMPTS`` allows. The consumer is
        then left without a pub/sub, and the next wait builds a new one.
        """
        try:
            yield
        except self._connection_errors:
            self._reconnect()

    def _reconnect_pubsub(self) -> None:
        """Close the pub/sub in use, build a new one, subscribe every awaited task on it, then catch up.

        The replaced pub/sub releases its connection here, so it never releases it from
        its finalizer: a finalizer run by the garbage collector on a thread inside the
        pool's lock would wait on that lock forever. The subscribe comes before the
        stored-state read, so a result written in between is published to the new
        subscription. A connection error closes the new pub/sub and leaves the consumer
        without one.
        """
        replaced, self._pubsub = self._pubsub, None
        if replaced is not None:
            replaced.close()
        self.backend.client.connection_pool.reset()
        pubsub = self.backend.client.pubsub(ignore_subscribe_messages=True)
        try:
            # With nothing awaited the pub/sub stays unconnected; its first subscribe connects it.
            if self.subscribed_to:
                pubsub.subscribe(*self.subscribed_to)
            self._pubsub = pubsub
            self.catch_up()
        except self._connection_errors:
            self._pubsub = None
            pubsub.close()
            raise

    def start(self, initial_task_id: str, **kwargs: Any) -> None:
        """Build the pub/sub for ``initial_task_id``, with every task still awaited on it."""
        if self.subscribed_to:
            self._reconnect()
            self._consume_from(initial_task_id)
        else:
            super().start(initial_task_id, **kwargs)

    def on_wait_for_pending(self, result: Any, **kwargs: Any) -> None:
        """Before a wait on ``result``, rebuild the pub/sub a failed reconnect left behind."""
        if self._pubsub is None and self.subscribed_to:
            self._reconnect()
        super().on_wait_for_pending(result, **kwargs)


class KitRedisBackend(RedisBackend):
    """celery's Redis result backend on a pool that carries the kit seam's connection behaviour.

    Its pool is a :class:`~tai42_kit.clients.impl.redis_connections.KitSyncConnectionPool`, over the
    connection class celery selects for the URL (TCP, TLS or a unix socket).
    """

    ResultConsumer = KitResultConsumer

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Build the backend from ``args`` / ``kwargs``, with its pools as kit sync pools."""
        super().__init__(*args, connection_pool=KitSyncConnectionPool, **kwargs)

    def _get_client(self) -> Callable[..., _ResultClient]:
        return functools.partial(_ResultClient, on_pubsub_reconnect=self._on_pubsub_reconnect)

    def _on_pubsub_reconnect(self) -> None:
        cast("KitResultConsumer", self.result_consumer).catch_up()

    def _get_pool(self, **params: Any) -> Any:
        options = redis_connection_options(
            celery_settings().result_backend_redis.connection_options(), sync=True, label=RESULT_BACKEND_LABEL
        )
        return super()._get_pool(**{**params, **options})

    def ensure(self, fun: Any, args: Any, **policy: Any) -> Any:
        """Run ``fun(*args)`` once.

        The kit seam owns this retry: every command and pub/sub read run under ``ensure``
        goes through the pool's connection retry (``CELERY_RESULT_BACKEND_RETRY_ATTEMPTS``,
        logged per retried failure), so celery's own ``retry_over_time`` loop is not run on
        top of it.
        """
        return fun(*args)


class KitLoader(AppLoader):
    """celery's app loader, with :class:`KitRedisBackend` for every ``redis`` / ``rediss`` backend URL."""

    override_backends = {"redis": KIT_REDIS_BACKEND, "rediss": KIT_REDIS_BACKEND}  # noqa: RUF012 - celery's own attribute shape


# --- broker --------------------------------------------------------------------


def _broker_identity(connection: kombu.Connection, defaults: Mapping[str, Any]) -> tuple[Any, ...]:
    """The broker a connection opens, with the transport's defaults filled in as kombu fills them."""
    return (
        connection.transport_cls,
        connection.hostname or defaults.get("hostname"),
        connection.userid or defaults.get("userid"),
        connection.password or defaults.get("password"),
        connection.virtual_host or defaults.get("virtual_host"),
        connection.port or defaults.get("port"),
    )


def _broker_label(connection: kombu.Connection, defaults: Mapping[str, Any]) -> str:
    """The env name of the broker URL setting ``connection`` was opened on.

    ``CELERY_BROKER_READ_URL`` / ``CELERY_BROKER_WRITE_URL`` when that var is set and
    names this broker, otherwise ``CELERY_BROKER_URL``. The comparison is on the parsed
    broker, so a clone (a producer pool's connection) keeps its side's name.
    """
    identity = _broker_identity(connection, defaults)
    for setting in (BROKER_READ_URL_SETTING, BROKER_WRITE_URL_SETTING):
        url = os.environ.get(setting)
        if url and _broker_identity(kombu.Connection(url), defaults) == identity:
            return setting
    return BROKER_URL_SETTING


class _PrefixedPubSub(PrefixedRedisPubSub, ReentrantPubSub):
    """kombu's key-prefixed pub/sub on the kit's re-entrant command lock."""


class _PrefixedRedis(PrefixedStrictRedis):
    """kombu's key-prefixed client, whose pub/sub is a :class:`_PrefixedPubSub`."""

    def pubsub(self, **kwargs: Any) -> _PrefixedPubSub:
        """A :class:`_PrefixedPubSub` on this client's pool, with its key prefix."""
        return _PrefixedPubSub(self.connection_pool, global_keyprefix=self.global_keyprefix, **kwargs)


def _polled_connection_class(connection_class: type, channel: RedisChannel) -> type:
    """The kit ``connection_class`` for ``channel``'s asynchronous pool, under its retry until the channel is polled.

    The asynchronous pool holds ``client`` and ``subclient``: the connections kombu polls
    for BRPOP and the fanout LISTEN, a worker's task and fanout reads and a control
    command's reply read alike. Before the channel registers with kombu's poller, at
    channel open, they are the kit connection under the kit retry, handshake included,
    which covers the channel's opening PING, so a worker's first broker connect and a
    publish's first channel. Once it is registered a connection acts as redis-py's own
    with no retry, each with its own ``Retry``, and retries no error: a connect makes one
    attempt, and on a drop it disconnects and raises. kombu takes the dropped socket off
    the poller and counts on that error reaching its caller: a worker's consumer restart
    registers it again, a control call fails. A retry that reconnected inside the read
    would leave a worker no longer consuming and lose a reply.

    ``connection_class`` is a kit sync connection class (see :func:`kit_sync_connection_class`);
    any other class raises ``TypeError``. Its ``retry`` stays the kit's own (the connection's
    retry view, with the kit budget) until the channel is registered.
    """
    if kit_sync_connection_class(connection_class) is not connection_class:
        raise TypeError(f"{connection_class.__qualname__} is not a kit sync connection class")
    kit_retry = inspect.getattr_static(connection_class, "retry")

    class PolledConnection(connection_class):
        _kit_retry_on_error: Any
        _own_retry: Retry

        @property
        def retry(self) -> Any:
            return self._own_retry if channel._registered else super().retry

        @retry.setter
        def retry(self, value: Any) -> None:
            kit_retry.__set__(self, value)
            # The retry redis-py gives a connection that has none: one attempt, a disconnect on its error.
            self._own_retry = Retry(NoBackoff(), 0)

        @property
        def retry_on_error(self) -> Any:
            return [] if channel._registered else self._kit_retry_on_error

        @retry_on_error.setter
        def retry_on_error(self, value: Any) -> None:
            self._kit_retry_on_error = value

    return PolledConnection


class KitRedisChannel(RedisChannel):
    """kombu's Redis channel, whose sync and async pools carry the kit seam's connection behaviour."""

    def _connparams(self, asynchronous: bool = False) -> dict[str, Any]:
        """The params of kombu's connections, with the kit's behaviour.

        The connection class is the kit's sync connection over kombu's own; the
        asynchronous pool's is wrapped once more (see :func:`_polled_connection_class`).
        """
        options = redis_connection_options(
            celery_settings().broker_redis.connection_options(), sync=True, label=self.connection.broker_label
        )
        params = {**super()._connparams(asynchronous), **options}
        params["connection_class"] = kit_sync_connection_class(params["connection_class"])
        if asynchronous:
            params["connection_class"] = _polled_connection_class(params["connection_class"], self)
        return params

    def _get_client(self) -> Callable[..., KitSyncRedis | _PrefixedRedis]:
        super()._get_client()  # kombu's redis-py version check
        if self.global_keyprefix:
            return functools.partial(_PrefixedRedis, global_keyprefix=self.global_keyprefix)
        return KitSyncRedis


class KitRedisTransport(RedisTransport):
    """kombu's Redis transport on :class:`KitRedisChannel`; carries the label of the URL it was built for."""

    Channel = KitRedisChannel

    def __init__(self, client: kombu.Connection, **kwargs: Any) -> None:
        """Build the transport for ``client`` and name the broker URL setting it serves."""
        super().__init__(client, **kwargs)
        self.broker_label = _broker_label(client, self.default_connection_params)


def _kit_transport_cls(transport_cls: Any) -> type:
    # kombu is untyped; its inferred return keeps the alias string it resolves before returning.
    resolved = cast("type", transport_cls)
    return KitRedisTransport if resolved is RedisTransport else resolved


class KitConnection(kombu.Connection):
    """A kombu connection that uses :class:`KitRedisTransport` wherever kombu resolves its own Redis transport.

    Its clones are kit connections too, and so are the connections its :meth:`Pool` hands
    out (celery's ``app.pool``, which every publish acquires from).
    """

    def get_transport_cls(self) -> type:
        """The transport class kombu resolves for this connection, its Redis transport replaced by the kit one."""
        return _kit_transport_cls(super().get_transport_cls())

    def Pool(self, limit: int | None = None, **kwargs: Any) -> KitConnectionPool:  # noqa: N802 - kombu's own method name
        """A pool of this connection's broker whose connections are kit connections."""
        return KitConnectionPool(self, limit, **kwargs)


class KitPooledConnection(PooledConnection):
    """kombu's pooled connection, with the :class:`KitRedisTransport` selection of :class:`KitConnection`."""

    def get_transport_cls(self) -> type:
        """The transport class kombu resolves for this connection, its Redis transport replaced by the kit one."""
        return _kit_transport_cls(super().get_transport_cls())


class KitConnectionPool(ConnectionPool):
    """kombu's connection pool, handing out :class:`KitPooledConnection` instances."""

    def new(self) -> KitPooledConnection:
        """A new pooled connection to the pool's broker."""
        return KitPooledConnection(self, **dict(self.connection._info(resolve=False)))


class KitAMQP(AMQP):
    """celery's AMQP facet, making every broker connection a :class:`KitConnection`."""

    Connection = KitConnection


# --- app wiring ----------------------------------------------------------------


def _unreached(resolved: type) -> str:
    return f"it opens {resolved.__name__}, which the kit's Redis connection does not reach; {_SEAM_FORMS}"


def _check_result_backend(app: Celery) -> None:
    url = app.conf.result_backend
    if not url:
        return
    scheme = _scheme(url)
    try:
        backend_cls, _ = by_url(url, app.loader)
    except (ImportError, ImproperlyConfigured) as exc:
        if scheme in _UNOPENABLE_SOCKET_SCHEMES:
            raise UnsupportedRedisUrlError(
                RESULT_BACKEND_LABEL, scheme, "celery has no such backend; a unix socket is redis+socket:///path"
            ) from exc
        raise
    if issubclass(backend_cls, RedisBackend) and backend_cls is not KitRedisBackend:
        raise UnsupportedRedisUrlError(RESULT_BACKEND_LABEL, scheme, _unreached(backend_cls))


def _broker_side_is_kit(connection_for_side: Any, url: str, setting: str) -> bool:
    """Whether a broker side opens a :class:`KitRedisTransport`; raises on a Redis form the seam cannot carry."""
    scheme = _scheme(url)
    try:
        connection = connection_for_side()
        transport_cls = connection.get_transport_cls()
    except KeyError as exc:
        if scheme in _UNOPENABLE_SOCKET_SCHEMES:
            raise UnsupportedRedisUrlError(
                setting, scheme, "kombu has no such transport; a unix socket is redis+socket:///path"
            ) from exc
        raise
    if not issubclass(transport_cls, RedisTransport):
        return False
    if transport_cls is not KitRedisTransport:
        raise UnsupportedRedisUrlError(setting, scheme, _unreached(transport_cls))
    if connection.alt:
        raise UnsupportedRedisUrlError(
            setting, scheme, f"a ';'-separated failover list is not supported; {_SEAM_FORMS}"
        )
    return True


def _check_redbeat_url(url: str) -> None:
    scheme = _scheme(url)
    if scheme not in _REDBEAT_SEAM_SCHEMES:
        raise UnsupportedRedisUrlError(REDBEAT_URL_SETTING, scheme, "use one redis://, rediss:// or unix:// URL")


def configure_redis_seam(app: Celery) -> None:
    """Check every Redis URL ``app`` uses and hand celery's own retries on a kit side to the seam.

    Raises :class:`UnsupportedRedisUrlError` for a Redis URL the seam cannot carry: a
    Sentinel backend or broker, a ``;`` failover list on a Redis broker, a ``unix://`` /
    ``socket://`` backend or broker URL (neither library opens one), and a RedBeat URL
    other than ``redis://``, ``rediss://`` or ``unix://``. A non-Redis broker or result
    backend is left to celery.

    The seam carries the worker's first broker connect and every publish, so on a side
    that resolves :class:`KitRedisTransport` celery's own loop is turned off:
    ``broker_connection_retry_on_startup`` for the read side and ``task_publish_retry``
    for the write side. celery's running-worker reconnect (``broker_connection_retry``)
    stays on: it reconnects the connections the worker's event loop polls, which raise
    on a drop instead of retrying.
    """
    _check_result_backend(app)
    _check_redbeat_url(app.conf.redbeat_redis_url)
    read_setting = BROKER_READ_URL_SETTING if os.environ.get(BROKER_READ_URL_SETTING) else BROKER_URL_SETTING
    write_setting = BROKER_WRITE_URL_SETTING if os.environ.get(BROKER_WRITE_URL_SETTING) else BROKER_URL_SETTING
    read_is_kit = _broker_side_is_kit(app.connection_for_read, app.conf.broker_read_url, read_setting)
    write_is_kit = _broker_side_is_kit(app.connection_for_write, app.conf.broker_write_url, write_setting)
    if read_is_kit:
        app.conf.broker_connection_retry_on_startup = False
    if write_is_kit:
        app.conf.task_publish_retry = False
