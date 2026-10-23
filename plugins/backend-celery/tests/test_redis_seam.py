"""The kit's Redis connection behaviour on the pools celery and kombu build.

The app picks the kit's result backend and broker transport for whatever Redis URL celery
resolves (its own env vars included), raises at creation on a Redis form the seam cannot
carry, and hands celery's startup and publish retries on a kit side to the seam.
"""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from celery.backends.rpc import RPCBackend
from kombu.transport.pyamqp import Transport as AmqpTransport
from kombu.transport.redis import PrefixedRedisPubSub
from tai42_kit.clients.impl.redis import KitSyncRedis, ReentrantPubSub, _LoggedSyncRetry

import tai42_backend_celery.core.app as app_module
from tai42_backend_celery.core.redis_seam import (
    KitRedisBackend,
    KitRedisChannel,
    KitRedisTransport,
    KitResultConsumer,
    UnsupportedRedisUrlError,
)
from tai42_backend_celery.core.settings import celery_settings

_AMQP = "amqp://localhost:5672//"


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Strip the celery, default-namespace and base retry vars so each test sets its own."""
    for key in list(os.environ):
        if key.startswith(("CELERY_", "TAI_DEFAULT_")) or key in {"RETRY_ATTEMPTS", "SOCKET_TIMEOUT"}:
            monkeypatch.delenv(key, raising=False)
    celery_settings.cache_clear()
    yield
    celery_settings.cache_clear()


def _app(monkeypatch: pytest.MonkeyPatch, **env: str) -> Any:
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    celery_settings.cache_clear()
    return app_module.create_celery_app()


def _channel(transport: KitRedisTransport) -> KitRedisChannel:
    """A channel of ``transport`` built without the PING its constructor sends."""
    channel = object.__new__(KitRedisChannel)
    channel.connection = transport
    return channel


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# --- selection, whichever env var names the URL -----------------------------------


def test_a_redis_result_backend_from_celerys_env_var_is_the_kit_backend(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_RESULT_BACKEND="redis://results.example:6390/3")

    assert isinstance(app.backend, KitRedisBackend)
    assert (app.backend.connparams["host"], app.backend.connparams["db"]) == ("results.example", 3)


def test_a_redis_broker_from_celerys_env_var_is_the_kit_transport(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL="redis://broker.example:6390/1")

    assert app.connection_for_write().get_transport_cls() is KitRedisTransport
    assert app.connection_for_read().get_transport_cls() is KitRedisTransport


def test_a_redis_read_side_alone_is_the_kit_transport(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL=_AMQP, CELERY_BROKER_READ_URL="redis://reads.example:6390/1")

    assert app.connection_for_read().get_transport_cls() is KitRedisTransport
    assert app.connection_for_write().get_transport_cls() is AmqpTransport


def test_a_redis_write_side_alone_is_the_kit_transport(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL=_AMQP, CELERY_BROKER_WRITE_URL="redis://writes.example:6390/1")

    assert app.connection_for_write().get_transport_cls() is KitRedisTransport
    assert app.connection_for_read().get_transport_cls() is AmqpTransport


def test_the_publish_pool_hands_out_kit_transport_connections(monkeypatch) -> None:
    app = _app(
        monkeypatch,
        CELERY_BROKER_URL="redis://shared.example:6390/0",
        CELERY_BROKER_WRITE_URL="redis://writes.example:6390/1",
        CELERY_BROKER_RETRY_ATTEMPTS="1",
    )

    connection = app.pool.acquire(block=True)
    try:
        assert isinstance(connection.transport, KitRedisTransport)
        assert _channel(connection.transport)._connparams()["retry"]._label == "CELERY_BROKER_WRITE_URL"
    finally:
        connection.release()


# --- URL forms ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "url", ["redis://h:6390/2", "rediss://h:6390/2?ssl_cert_reqs=required", "redis+socket:///tmp/r.sock"]
)
def test_the_seam_forms_reach_the_kit_classes(monkeypatch, url: str) -> None:
    app = _app(monkeypatch, CELERY_RESULT_BACKEND=url, CELERY_BROKER_URL=url)

    assert isinstance(app.backend, KitRedisBackend)
    assert app.connection_for_read().get_transport_cls() is KitRedisTransport


@pytest.mark.parametrize("url", ["unix:///tmp/r.sock", "socket:///tmp/r.sock"])
@pytest.mark.parametrize("setting", ["CELERY_RESULT_BACKEND", "CELERY_BROKER_URL"])
def test_a_unix_url_names_the_setting_and_the_scheme(monkeypatch, url: str, setting: str) -> None:
    with pytest.raises(UnsupportedRedisUrlError) as caught:
        _app(monkeypatch, **{setting: url})

    scheme = url.partition("://")[0]
    assert caught.value.setting == setting
    assert caught.value.scheme == scheme
    assert setting in str(caught.value)
    assert f"{scheme}://" in str(caught.value)
    assert "/tmp/r.sock" not in str(caught.value)


@pytest.mark.parametrize("setting", ["CELERY_RESULT_BACKEND", "CELERY_BROKER_URL"])
def test_a_sentinel_url_names_the_setting_and_not_the_url(monkeypatch, setting: str) -> None:
    with pytest.raises(UnsupportedRedisUrlError) as caught:
        _app(monkeypatch, **{setting: "sentinel://:hunter2@h:26379"})

    assert caught.value.setting == setting
    assert "hunter2" not in str(caught.value)


@pytest.mark.parametrize("setting", ["CELERY_BROKER_READ_URL", "CELERY_BROKER_WRITE_URL"])
def test_a_sentinel_side_url_names_that_side(monkeypatch, setting: str) -> None:
    with pytest.raises(UnsupportedRedisUrlError) as caught:
        _app(monkeypatch, CELERY_BROKER_URL=_AMQP, **{setting: "sentinel://h:26379"})

    assert caught.value.setting == setting


def test_a_broker_failover_list_names_the_broker_setting(monkeypatch) -> None:
    with pytest.raises(UnsupportedRedisUrlError) as caught:
        _app(monkeypatch, CELERY_BROKER_URL="redis://:hunter2@a:6379/0;redis://b:6379/0")

    assert caught.value.setting == "CELERY_BROKER_URL"
    assert "failover" in str(caught.value)
    assert "hunter2" not in str(caught.value)


@pytest.mark.parametrize("url", ["redis://h:6390/2", "rediss://h:6390/2", "unix:///tmp/r.sock"])
def test_the_redbeat_seam_forms_are_accepted(monkeypatch, url: str) -> None:
    app = _app(monkeypatch, CELERY_REDBEAT_REDIS_URL=url)

    assert app.conf.redbeat_redis_url == url


@pytest.mark.parametrize("url", ["redis-cluster://h:30001", "redis-sentinel://h:26379"])
def test_a_redbeat_cluster_or_sentinel_url_raises(monkeypatch, url: str) -> None:
    with pytest.raises(UnsupportedRedisUrlError) as caught:
        _app(monkeypatch, CELERY_REDBEAT_REDIS_URL=url)

    assert caught.value.setting == "CELERY_REDBEAT_REDIS_URL"


def test_an_amqp_broker_and_an_rpc_backend_keep_celerys_classes_and_defaults(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL=_AMQP, CELERY_RESULT_BACKEND="rpc://")

    assert type(app.backend) is RPCBackend
    assert app.connection_for_read().get_transport_cls() is AmqpTransport
    assert app.conf.broker_connection_retry is True
    assert app.conf.broker_connection_retry_on_startup is None
    assert app.conf.task_publish_retry is True


# --- result backend -----------------------------------------------------------------


def test_the_result_backend_pool_carries_its_own_namespace(monkeypatch) -> None:
    app = _app(
        monkeypatch,
        CELERY_RESULT_BACKEND="redis://results.example:6390/3",
        CELERY_RESULT_BACKEND_RETRY_ATTEMPTS="2",
        CELERY_RESULT_BACKEND_SOCKET_TIMEOUT="3",
        RETRY_ATTEMPTS="5",
    )

    connection_kwargs = app.backend.client.connection_pool.connection_kwargs
    retry = connection_kwargs["retry"]
    assert isinstance(retry, _LoggedSyncRetry)
    assert retry._budget == 2
    assert retry._label == "CELERY_RESULT_BACKEND"
    assert connection_kwargs["retry_on_error"] == [redis.ConnectionError, redis.TimeoutError, OSError]
    assert connection_kwargs["socket_timeout"] == 3
    assert (connection_kwargs["host"], connection_kwargs["db"]) == ("results.example", 3)


def test_the_result_backend_pubsub_reconnects_under_the_seam_and_then_catches_up(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_RESULT_BACKEND="redis://results.example:6390/3")
    consumer = app.backend.result_consumer
    caught_up: list[bool] = []
    monkeypatch.setattr(consumer, "catch_up", lambda: caught_up.append(True))

    assert isinstance(consumer, KitResultConsumer)
    assert isinstance(app.backend.client, KitSyncRedis)
    pubsub = app.backend.client.pubsub()
    assert isinstance(pubsub, ReentrantPubSub)

    pubsub.on_connect(object())  # nothing subscribed yet, so nothing is sent on the connection

    assert caught_up == [True]


def test_the_result_consumer_catch_up_delivers_every_stored_state(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_RESULT_BACKEND="redis://results.example:6390/3")
    consumer = app.backend.result_consumer
    meta = {"task_id": "t1", "status": "SUCCESS", "result": 5}
    delivered: list[Any] = []
    consumer.subscribed_to = {b"celery-task-meta-t1", b"celery-task-meta-t2"}
    monkeypatch.setattr(app.backend.client, "mget", lambda keys: [app.backend.encode(meta), None])
    monkeypatch.setattr(consumer, "on_state_change", lambda state, message: delivered.append((state, message)))

    consumer.catch_up()

    assert delivered == [(meta, None)]


def test_a_failed_result_consumer_reconnect_raises_its_connection_error(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_RESULT_BACKEND="redis://results.example:6390/3")
    consumer = app.backend.result_consumer
    refused = redis.ConnectionError("refused")

    def reconnect() -> None:
        raise refused

    monkeypatch.setattr(consumer, "_reconnect", reconnect)

    with pytest.raises(redis.ConnectionError) as raised, consumer.reconnect_on_error():
        raise redis.ConnectionError("dropped")

    assert raised.value is refused


def test_an_unset_result_backend_namespace_keeps_celerys_defaults(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_RESULT_BACKEND="redis://results.example:6390/3")

    connection_kwargs = app.backend.client.connection_pool.connection_kwargs
    assert connection_kwargs["socket_timeout"] == 120.0
    assert "retry" not in connection_kwargs


def test_ensure_runs_once_and_lets_the_error_through(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_RESULT_BACKEND="redis://results.example:6390/3")
    calls: list[tuple[Any, ...]] = []

    def failing(*args: Any) -> None:
        calls.append(args)
        raise redis.ConnectionError("down")

    with pytest.raises(redis.ConnectionError):
        app.backend.ensure(failing, ("k", "v"))

    assert calls == [("k", "v")]


# --- broker -------------------------------------------------------------------------


def _retry_keys(app: Any) -> tuple[Any, Any, Any]:
    conf = app.conf
    return conf.broker_connection_retry, conf.broker_connection_retry_on_startup, conf.task_publish_retry


def test_a_redis_broker_hands_startup_and_publish_retry_to_the_seam(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL="redis://broker.example:6390/1")

    assert _retry_keys(app) == (True, False, False)


def test_a_redis_read_side_hands_only_the_startup_retry_to_the_seam(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL=_AMQP, CELERY_BROKER_READ_URL="redis://reads.example:6390/1")

    assert _retry_keys(app) == (True, False, True)


def test_a_redis_write_side_hands_only_the_publish_retry_to_the_seam(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL=_AMQP, CELERY_BROKER_WRITE_URL="redis://writes.example:6390/1")

    assert _retry_keys(app) == (True, None, False)


def test_an_amqp_broker_keeps_celerys_retry_keys(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL=_AMQP)

    assert _retry_keys(app) == (True, None, True)


@pytest.mark.parametrize("asynchronous", [False, True])
def test_the_broker_connparams_carry_the_seam_and_keep_kombus_identity(monkeypatch, asynchronous: bool) -> None:
    app = _app(
        monkeypatch,
        CELERY_BROKER_URL="redis://:s3cret@broker.example:6390/4",
        CELERY_BROKER_RETRY_ATTEMPTS="2",
        CELERY_BROKER_SOCKET_TIMEOUT="3",
    )
    params = _channel(app.connection_for_write().transport)._connparams(asynchronous=asynchronous)

    assert isinstance(params["retry"], _LoggedSyncRetry)
    assert params["retry"]._budget == 2
    assert params["retry"]._label == "CELERY_BROKER_URL"
    assert params["socket_timeout"] == 3
    assert (params["host"], params["port"], params["db"], params["password"]) == ("broker.example", 6390, 4, "s3cret")
    if asynchronous:
        # A subclass of kombu's poller-aware connection class, defined inside its ``_connparams``.
        kombu_class = params["connection_class"].__mro__[1]
        assert kombu_class.__module__ == "kombu.transport.redis"
        assert kombu_class is not redis.Connection
        assert issubclass(kombu_class, redis.Connection)
    else:
        assert params["connection_class"] is redis.Connection


def test_a_polled_connection_runs_under_the_seam_retry_until_its_channel_is_registered(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL="redis://broker.example:6390/1", CELERY_BROKER_RETRY_ATTEMPTS="2")
    channel = _channel(app.connection_for_read().transport)
    channel._registered = False
    params = channel._connparams(asynchronous=True)
    connection = params["connection_class"](
        host=params["host"], port=params["port"], retry=params["retry"], retry_on_error=params["retry_on_error"]
    )

    # The channel's opening PING runs under the seam retry.
    assert isinstance(connection.retry, _LoggedSyncRetry)
    assert connection.retry._label == "CELERY_BROKER_URL"
    assert redis.ConnectionError in connection.retry_on_error

    # Once kombu's poller holds the channel, a drop disconnects and raises, as redis-py's own connection does.
    channel._registered = True
    assert connection.retry._retries == 0
    assert connection.retry_on_error == []

    # The synchronous pool's connections keep the retry throughout.
    assert channel._connparams()["connection_class"] is redis.Connection


def test_each_registered_polled_connection_carries_its_own_retry(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL="redis://broker.example:6390/1", CELERY_BROKER_RETRY_ATTEMPTS="2")
    channel = _channel(app.connection_for_read().transport)
    channel._registered = True
    params = channel._connparams(asynchronous=True)
    first, second = (
        params["connection_class"](host=params["host"], port=params["port"], retry=params["retry"]) for _ in range(2)
    )

    # redis-py updates a connection's retry in place, so no two connections share one.
    first.retry.update_supported_errors([KeyError])

    assert first.retry is not second.retry
    assert KeyError not in second.retry._supported_errors


def test_the_broker_client_pubsub_takes_the_kits_reentrant_lock(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL="redis://broker.example:6390/1")
    channel = _channel(app.connection_for_read().transport)
    channel.global_keyprefix = ""

    client_cls = channel._get_client()

    assert client_cls is KitSyncRedis
    assert isinstance(client_cls().pubsub(), ReentrantPubSub)


def test_a_prefixed_broker_client_pubsub_keeps_its_prefix_and_takes_the_reentrant_lock(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL="redis://broker.example:6390/1")
    channel = _channel(app.connection_for_read().transport)
    channel.global_keyprefix = "acme:"

    client = channel._get_client()()
    pubsub = client.pubsub()

    assert isinstance(pubsub, PrefixedRedisPubSub)
    assert isinstance(pubsub, ReentrantPubSub)
    assert pubsub.global_keyprefix == "acme:"


def test_a_unix_socket_broker_keeps_kombus_socket_connection(monkeypatch) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL="redis+socket:///tmp/r.sock", CELERY_BROKER_SOCKET_CONNECT_TIMEOUT="2")
    params = _channel(app.connection_for_write().transport)._connparams()

    assert params["connection_class"] is redis.UnixDomainSocketConnection
    assert params["path"] == "/tmp/r.sock"
    assert params["socket_connect_timeout"] == 2


@pytest.mark.parametrize(
    ("setting", "side"), [("CELERY_BROKER_READ_URL", "read"), ("CELERY_BROKER_WRITE_URL", "write")]
)
def test_a_side_channel_is_labelled_with_its_side_setting(monkeypatch, setting: str, side: str) -> None:
    app = _app(
        monkeypatch,
        CELERY_BROKER_URL="redis://shared.example:6390/0",
        CELERY_BROKER_RETRY_ATTEMPTS="1",
        **{setting: f"redis://{side}.example:6390/1"},
    )
    side_connection = app.connection_for_read() if side == "read" else app.connection_for_write()
    other_connection = app.connection_for_write() if side == "read" else app.connection_for_read()

    assert _channel(side_connection.transport)._connparams()["retry"]._label == setting
    assert _channel(side_connection.clone().transport)._connparams()["retry"]._label == setting
    assert _channel(other_connection.transport)._connparams()["retry"]._label == "CELERY_BROKER_URL"


def test_a_broker_connect_failure_is_retried_and_logged_by_the_seam(monkeypatch, caplog) -> None:
    app = _app(monkeypatch, CELERY_BROKER_URL=f"redis://127.0.0.1:{_closed_port()}/0", CELERY_BROKER_RETRY_ATTEMPTS="2")
    connection = app.connection_for_write()
    transport = connection.transport
    assert isinstance(transport, KitRedisTransport)

    with caplog.at_level(logging.WARNING), pytest.raises(redis.ConnectionError):
        transport.create_channel(transport)

    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert all("CELERY_BROKER_URL" in record.getMessage() for record in warnings)
