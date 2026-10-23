"""Live verification: the kit's Redis result backend and broker transport carry real traffic.

An app on a real Redis as broker and result backend sends a task to an in-process worker
and reads its result. A second test runs the worker and the client through a TCP relay
the test controls, closes it while the worker waits on its blocking read, and shows that
celery's own reconnect loop (logged at ERROR) brings the worker back once Redis answers
again. Two more run the result consumer alone through the relay: a result written while
its pub/sub reconnects is still delivered, and a reconnect past its retry raises the
connection error.

Point ``TAI_TEST_CELERY_BROKER`` at a Redis broker to run it; once set, an unreachable
broker fails the module instead of skipping it. Unset, it probes
``redis://localhost:6399/0`` and skips when nothing answers there.
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import socket
import threading
import time
import uuid
from collections.abc import Iterator
from typing import Any, cast
from urllib.parse import urlsplit

import pytest
import redis
from celery import states
from kombu import pools

from tai42_backend_celery.core.redis_seam import KitRedisBackend, KitRedisTransport
from tai42_backend_celery.core.settings import celery_settings

_BROKER_ENV = "TAI_TEST_CELERY_BROKER"
_BROKER_REQUIRED = _BROKER_ENV in os.environ
_BROKER = os.environ.get(_BROKER_ENV, "redis://localhost:6399/0")


def _redis_reachable(url: str) -> bool:
    """Whether a Redis broker answers at ``url``; a configured broker that does not answer raises."""
    try:
        redis.Redis.from_url(url).ping()
    except redis.ConnectionError:
        if _BROKER_REQUIRED:
            raise
        return False
    return True


pytestmark = [
    pytest.mark.skipif(not _redis_reachable(_BROKER), reason=f"no Redis broker reachable at {_BROKER}"),
    # A live worker boot legitimately emits framework warnings; do not treat them
    # as errors for these integration tests.
    pytest.mark.filterwarnings("ignore"),
]


class _Relay:
    """A loopback TCP forwarder to one upstream, which can be closed and reopened on the same port."""

    def __init__(self, upstream: tuple[str, int]) -> None:
        self._upstream = upstream
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._listener: socket.socket | None = None
        self.port = 0

    def open(self) -> None:
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen()
        self.port = listener.getsockname()[1]
        self._listener = listener
        threading.Thread(target=self._accept, args=(listener,), daemon=True).start()

    def close(self) -> None:
        """Stop listening and close every open socket, as a Redis restart does."""
        with self._lock:
            sockets, self._sockets = self._sockets, []
            listener, self._listener = self._listener, None
        for sock in [listener, *sockets]:
            if sock is not None:
                # A socket the far side already shut down refuses a second shutdown.
                with contextlib.suppress(OSError):
                    sock.shutdown(socket.SHUT_RDWR)
                sock.close()

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                client, _ = listener.accept()
            except OSError:
                return
            upstream = socket.create_connection(self._upstream)
            with self._lock:
                self._sockets += [client, upstream]
            threading.Thread(target=self._pipe, args=(client, upstream), daemon=True).start()
            threading.Thread(target=self._pipe, args=(upstream, client), daemon=True).start()

    @staticmethod
    def _pipe(source: socket.socket, target: socket.socket) -> None:
        # Closing the relay breaks its pipes; a broken pipe ends the forwarding, as a dropped link does.
        with contextlib.suppress(OSError):
            while data := source.recv(65536):
                target.sendall(data)
        for sock in (source, target):
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _app_on(monkeypatch: pytest.MonkeyPatch, url: str, *, result_retry_attempts: int = 2) -> Any:
    """A fresh plugin app on ``url`` as broker and result backend, with the seam's retry on."""
    from tai42_backend_celery.core.app import create_celery_app

    monkeypatch.setenv("CELERY_BROKER_URL", url)
    monkeypatch.setenv("CELERY_RESULT_BACKEND", url)
    monkeypatch.setenv("CELERY_BROKER_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("CELERY_RESULT_BACKEND_RETRY_ATTEMPTS", str(result_retry_attempts))
    for key in ("CELERY_BROKER_READ_URL", "CELERY_BROKER_WRITE_URL"):
        monkeypatch.delenv(key, raising=False)
    celery_settings.cache_clear()
    # kombu keeps its connection and producer pools process-wide, keyed by broker; an
    # earlier app on the same broker would hand this app connections built with its settings.
    pools.reset()
    app = create_celery_app()
    app.conf.task_default_queue = f"tai42-live-seam-{uuid.uuid4().hex}"

    @app.task(name="tests.live_seam_add")
    def add(x: int, y: int) -> int:
        return x + y

    return app


@pytest.fixture
def run_worker() -> Iterator[Any]:
    """Start a solo-pool worker for an app in a background thread; stopped at teardown."""
    from celery import _state
    from celery.worker import state

    threads: list[threading.Thread] = []
    apps: list[Any] = []

    def start(app: Any) -> None:
        apps.append(app)
        worker = app.Worker(
            pool="solo",
            concurrency=1,
            without_gossip=True,
            without_mingle=True,
            without_heartbeat=True,
            loglevel="ERROR",
            quiet=True,
        )
        thread = threading.Thread(target=worker.start, name="live-seam-worker", daemon=True)
        thread.start()
        threads.append(thread)

    try:
        yield start
    finally:
        state.should_stop = 0
        for thread in threads:
            thread.join(timeout=15)
        state.should_stop = None
        # A collected result unsubscribes from its app's result channel, so results are
        # collected while their app is still connected; then each app's connections close.
        gc.collect()
        for app in apps:
            app.backend.result_consumer.stop()
            app.backend.client.connection_pool.disconnect()
            app.close()
        # Hosting a worker in-process arms Celery's "never call get() in a task"
        # guard process-wide; disarm it so later tests can collect results.
        _state._set_task_join_will_block(False)
        celery_settings.cache_clear()


def _result(app: Any, timeout: float) -> int:
    return app.send_task("tests.live_seam_add", args=(2, 3)).get(timeout=timeout, disable_sync_subtasks=False)


def test_a_task_round_trips_over_the_kit_backend_and_transport(monkeypatch, run_worker) -> None:
    app = _app_on(monkeypatch, _BROKER)
    run_worker(app)

    assert _result(app, timeout=30) == 5

    assert isinstance(app.backend, KitRedisBackend)
    assert app.backend.client.connection_pool.connection_kwargs["retry"]._label == "CELERY_RESULT_BACKEND"
    with app.pool.acquire(block=True) as connection:
        assert isinstance(connection.transport, KitRedisTransport)
        pool_kwargs = connection.default_channel.client.connection_pool.connection_kwargs
        assert pool_kwargs["retry"]._label == "CELERY_BROKER_URL"


def test_a_redis_restart_under_a_waiting_worker_is_recovered_by_celerys_loop(monkeypatch, run_worker) -> None:
    upstream = urlsplit(_BROKER)
    relay = _Relay((upstream.hostname or "localhost", upstream.port or 6379))
    relay.open()
    records = _Records()
    consumer_logger = logging.getLogger("celery.worker.consumer.consumer")
    consumer_logger.addHandler(records)
    try:
        app = _app_on(monkeypatch, f"redis://127.0.0.1:{relay.port}{upstream.path or '/0'}")
        run_worker(app)
        assert _result(app, timeout=30) == 5

        # The worker now waits on its blocking read; the relay going away is a Redis restart.
        relay.close()
        deadline = time.monotonic() + 30
        while not any("Cannot connect" in message for message in records.messages):
            assert time.monotonic() < deadline, f"celery logged no reconnect error: {records.messages}"
            time.sleep(0.1)

        relay.open()
        assert _result(app, timeout=40) == 5
    finally:
        consumer_logger.removeHandler(records)
        relay.close()


def _drop_fanout_links(url: str) -> int:
    """Kill every pattern-subscribed client on ``url`` (the worker's fanout LISTEN link); returns how many."""
    direct = redis.Redis.from_url(url)
    try:
        clients = cast("list[dict[str, Any]]", direct.client_list())
        listeners = [client for client in clients if int(client.get("psub", 0)) > 0]
        for client in listeners:
            direct.client_kill_filter(_id=client["id"])
        return len(listeners)
    finally:
        direct.close()


def test_a_worker_whose_fanout_link_drops_keeps_consuming_tasks(monkeypatch, run_worker) -> None:
    # Only the LISTEN connection drops. kombu takes that socket off its event loop and
    # counts on the error reaching celery, whose consumer restart registers it again.
    app = _app_on(monkeypatch, _BROKER)
    run_worker(app)
    assert _result(app, timeout=30) == 5

    assert _drop_fanout_links(_BROKER) >= 1

    assert _result(app, timeout=30) == 5


def _drop_every_other_client(url: str) -> int:
    """Kill every client on ``url`` but the connection doing the killing; returns how many."""
    direct = redis.Redis.from_url(url)
    try:
        own = direct.client_id()
        clients = cast("list[dict[str, Any]]", direct.client_list())
        others = [client for client in clients if int(client["id"]) != own]
        for client in others:
            direct.client_kill_filter(_id=client["id"])
        return len(others)
    finally:
        direct.close()


def test_a_control_reply_read_that_drops_raises_and_the_next_call_reconnects(monkeypatch, run_worker) -> None:
    # A control reply read is polled by kombu, so the broker retry never reconnects it.
    app = _app_on(monkeypatch, _BROKER)
    run_worker(app)
    assert _result(app, timeout=30) == 5
    inspect = app.control.inspect(timeout=5)
    assert inspect.ping()

    assert _drop_every_other_client(_BROKER) >= 1

    with pytest.raises(redis.ConnectionError):
        inspect.ping()
    # The worker's own links dropped too; once its consumer restart serves a task, the next call reconnects.
    assert _result(app, timeout=30) == 5
    assert inspect.ping()


@contextlib.contextmanager
def _relayed_app(monkeypatch: pytest.MonkeyPatch, *, result_retry_attempts: int) -> Iterator[tuple[Any, _Relay]]:
    """A plugin app on the broker through a relay the test controls; closed at exit."""
    upstream = urlsplit(_BROKER)
    relay = _Relay((upstream.hostname or "localhost", upstream.port or 6379))
    relay.open()
    app = _app_on(
        monkeypatch,
        f"redis://127.0.0.1:{relay.port}{upstream.path or '/0'}",
        result_retry_attempts=result_retry_attempts,
    )
    try:
        yield app, relay
    finally:
        consumer = app.backend.result_consumer
        consumer.stop()
        # A result still held by a captured log record (its retried error's traceback
        # pins the wait) is collected after this test, and a collected result
        # unsubscribes on the consumer's pub/sub: with none left, it sends nothing,
        # instead of retrying against the closed relay inside a later test.
        consumer._pubsub = None
        app.backend.client.connection_pool.disconnect()
        app.close()
        relay.close()
        celery_settings.cache_clear()


def _wait_in_thread(result: Any) -> tuple[threading.Thread, dict[str, Any]]:
    """Wait on ``result`` in a daemon thread, so a wait that never returns fails the test instead of hanging it."""
    outcome: dict[str, Any] = {}

    def wait() -> None:
        try:
            outcome["value"] = result.get(timeout=30, disable_sync_subtasks=False)
        except Exception as exc:  # the test asserts on the error the wait raised
            outcome["error"] = exc

    thread = threading.Thread(target=wait, name="live-seam-waiter", daemon=True)
    thread.start()
    return thread, outcome


def _wait_subscribed(direct: Any, key: bytes) -> None:
    deadline = time.monotonic() + 10
    while not direct.pubsub_numsub(key)[0][1]:
        assert time.monotonic() < deadline, "the result consumer never subscribed"
        time.sleep(0.05)


def test_a_result_written_while_the_consumer_reconnects_is_delivered(monkeypatch) -> None:
    direct = redis.Redis.from_url(_BROKER)
    with _relayed_app(monkeypatch, result_retry_attempts=30) as (app, relay):
        task_id = uuid.uuid4().hex
        key = app.backend.get_key_for_task(task_id)
        waiter, outcome = _wait_in_thread(app.AsyncResult(task_id))
        _wait_subscribed(direct, key)

        # The consumer's link drops; the task's result is written while it reconnects.
        relay.close()
        meta = {**app.backend._get_result_meta(5, states.SUCCESS, None, None), "task_id": task_id}
        direct.set(key, app.backend.encode(meta))
        relay.open()

        waiter.join(20)
        assert outcome == {"value": 5}
    direct.delete(key)
    direct.close()


def test_a_reconnect_past_its_retry_raises_the_connection_error(monkeypatch) -> None:
    direct = redis.Redis.from_url(_BROKER)
    with _relayed_app(monkeypatch, result_retry_attempts=2) as (app, relay):
        task_id = uuid.uuid4().hex
        waiter, outcome = _wait_in_thread(app.AsyncResult(task_id))
        _wait_subscribed(direct, app.backend.get_key_for_task(task_id))

        relay.close()

        waiter.join(20)
        assert isinstance(outcome.get("error"), redis.ConnectionError), outcome
    direct.close()


def _publish_result(direct: Any, app: Any, task_id: str, *, store: bool) -> None:
    """Publish ``task_id``'s result as the backend does; ``store`` also writes its stored state."""
    meta = {**app.backend._get_result_meta(5, states.SUCCESS, None, None), "task_id": task_id}
    encoded = app.backend.encode(meta)
    if store:
        direct.set(app.backend.get_key_for_task(task_id), encoded)
    direct.publish(app.backend.get_key_for_task(task_id), encoded)


def _fail_a_wait(direct: Any, app: Any, relay: _Relay, task_id: str) -> Any:
    """Wait on ``task_id`` while the relay goes away; the wait's reconnect fails. Returns the awaited result."""
    result = app.AsyncResult(task_id)
    waiter, outcome = _wait_in_thread(result)
    _wait_subscribed(direct, app.backend.get_key_for_task(task_id))
    relay.close()
    waiter.join(20)
    assert isinstance(outcome.get("error"), redis.ConnectionError), outcome
    relay.open()
    return result


def test_a_wait_after_a_failed_reconnect_subscribes_its_already_pending_task(monkeypatch) -> None:
    direct = redis.Redis.from_url(_BROKER)
    with _relayed_app(monkeypatch, result_retry_attempts=0) as (app, relay):
        task_id = uuid.uuid4().hex
        pending = _fail_a_wait(direct, app, relay, task_id)

        # A second wait on the same result: its task is already pending, so no new subscribe is sent for it.
        waiter, outcome = _wait_in_thread(pending)
        _wait_subscribed(direct, app.backend.get_key_for_task(task_id))
        _publish_result(direct, app, task_id, store=True)

        waiter.join(20)
        assert outcome == {"value": 5}
        direct.delete(app.backend.get_key_for_task(task_id))
    direct.close()


def test_a_new_wait_after_a_failed_reconnect_resubscribes_every_pending_task(monkeypatch) -> None:
    direct = redis.Redis.from_url(_BROKER)
    with _relayed_app(monkeypatch, result_retry_attempts=0) as (app, relay):
        pending_id, new_id = uuid.uuid4().hex, uuid.uuid4().hex
        pending = _fail_a_wait(direct, app, relay, pending_id)

        waiter, outcome = _wait_in_thread(app.AsyncResult(new_id))
        _wait_subscribed(direct, app.backend.get_key_for_task(pending_id))
        # Published only: the pending task's result reaches it through the subscription alone.
        _publish_result(direct, app, pending_id, store=False)
        _publish_result(direct, app, new_id, store=True)

        waiter.join(20)
        assert outcome == {"value": 5}
        assert pending.get(timeout=5, disable_sync_subtasks=False) == 5
        direct.delete(app.backend.get_key_for_task(new_id))
    direct.close()


def test_a_wait_after_a_failed_reconnect_catches_up_a_result_stored_in_the_outage(monkeypatch) -> None:
    direct = redis.Redis.from_url(_BROKER)
    with _relayed_app(monkeypatch, result_retry_attempts=0) as (app, relay):
        pending_id, new_id = uuid.uuid4().hex, uuid.uuid4().hex
        pending = _fail_a_wait(direct, app, relay, pending_id)
        meta = {**app.backend._get_result_meta(5, states.SUCCESS, None, None), "task_id": pending_id}
        direct.set(app.backend.get_key_for_task(pending_id), app.backend.encode(meta))

        waiter, outcome = _wait_in_thread(app.AsyncResult(new_id))
        _publish_result(direct, app, new_id, store=True)
        waiter.join(20)

        assert outcome == {"value": 5}
        assert pending._cache is not None
        assert pending._cache["status"] == states.SUCCESS
        for task_id in (pending_id, new_id):
            direct.delete(app.backend.get_key_for_task(task_id))
    direct.close()


def test_a_pubsub_rebuilt_with_nothing_awaited_still_catches_up_after_a_drop(monkeypatch) -> None:
    direct = redis.Redis.from_url(_BROKER)
    with _relayed_app(monkeypatch, result_retry_attempts=30) as (app, relay):
        consumer = app.backend.result_consumer
        # A connection error with no task awaited rebuilds the pub/sub with nothing to subscribe.
        with consumer.reconnect_on_error():
            raise redis.ConnectionError("link lost")
        assert consumer._pubsub is not None

        # A later wait subscribes on that pub/sub; its link drops and the result is stored meanwhile.
        task_id = uuid.uuid4().hex
        key = app.backend.get_key_for_task(task_id)
        waiter, outcome = _wait_in_thread(app.AsyncResult(task_id))
        _wait_subscribed(direct, key)
        relay.close()
        meta = {**app.backend._get_result_meta(5, states.SUCCESS, None, None), "task_id": task_id}
        direct.set(key, app.backend.encode(meta))
        relay.open()

        waiter.join(20)
        assert outcome == {"value": 5}
    direct.delete(key)
    direct.close()
