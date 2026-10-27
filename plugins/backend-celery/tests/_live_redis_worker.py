"""The live worker harness: a plugin app on a real Redis, an in-process worker, and a TCP relay.

Point ``TAI_TEST_CELERY_BROKER`` at a Redis broker to run the live modules; once set, an
unreachable broker fails them instead of skipping them. Unset, they probe
``redis://localhost:6399/0`` and skip when nothing answers there. The ``run_worker`` fixture
is registered for every module by ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import socket
import threading
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import redis
from kombu import pools

from tai42_backend_celery.core.settings import celery_settings
from tai42_backend_celery.core.tasks import AsyncTask

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


def _live_marks() -> list[pytest.MarkDecorator]:
    """The marks of a module that drives a live worker: skipped when no broker answers.

    A live worker boot legitimately emits framework warnings; they are not errors for these
    integration tests.
    """
    return [
        pytest.mark.skipif(not _redis_reachable(_BROKER), reason=f"no Redis broker reachable at {_BROKER}"),
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

    @app.task(name="tests.live_async_sleep", base=AsyncTask, bind=True)
    def async_sleep(self: AsyncTask, seconds: float) -> float:
        """A task body that runs on the task's own loop, as every tai task body does."""
        return self.run_async(asyncio.sleep(seconds, result=seconds))

    return app


@pytest.fixture
def run_worker() -> Iterator[Any]:
    """Start a worker for an app in a background thread, solo unless the options say otherwise; stopped at teardown."""
    from celery import _state
    from celery.worker import state

    threads: list[threading.Thread] = []
    apps: list[Any] = []

    def start(app: Any, **options: Any) -> Any:
        apps.append(app)
        worker = app.Worker(
            **{
                "pool": "solo",
                "concurrency": 1,
                "without_gossip": True,
                "without_mingle": True,
                "without_heartbeat": True,
                "loglevel": "ERROR",
                "quiet": True,
                **options,
            }
        )
        thread = threading.Thread(target=worker.start, name="live-seam-worker", daemon=True)
        thread.start()
        threads.append(thread)
        return worker

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
