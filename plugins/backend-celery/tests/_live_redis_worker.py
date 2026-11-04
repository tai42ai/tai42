"""The live worker harness: a plugin app on a real Redis, an in-process worker, and a TCP relay.

The relay forwards in its own process (``_tcp_relay.py``), driven as a context manager.

Point ``TAI_TEST_CELERY_BROKER`` at a Redis broker to run the live modules; unset, they are
skipped, and once set, an unreachable broker fails them. The ``run_worker`` fixture is
registered for every module by ``conftest.py``.
"""

from __future__ import annotations

import asyncio
import gc
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

import pytest
import redis
from kombu import pools

from tai42_backend_celery.core import liveness_step
from tai42_backend_celery.core.settings import celery_settings
from tai42_backend_celery.core.tasks import AsyncTask

_BROKER_ENV = "TAI_TEST_CELERY_BROKER"
_BROKER = os.environ.get(_BROKER_ENV, "")
_RELAY_PROGRAM = Path(__file__).with_name("_tcp_relay.py")


def _live_marks() -> list[pytest.MarkDecorator]:
    """The marks of a module that drives a live worker: skipped when no broker is configured.

    A configured broker is pinged here, so one that does not answer fails the module loudly.
    A live worker boot legitimately emits framework warnings; they are not errors for these
    integration tests.
    """
    if _BROKER:
        redis.Redis.from_url(_BROKER).ping()
    return [
        pytest.mark.skipif(not _BROKER, reason=f"{_BROKER_ENV} is unset"),
        pytest.mark.filterwarnings("ignore"),
    ]


class _Relay:
    """A loopback TCP forwarder to one upstream, which can be closed and reopened on the same port.

    It forwards in its own process (``_tcp_relay.py``), so no finalizer the garbage collector
    runs in this process (a collected result unsubscribing over the relay) can block the
    forwarding it waits on. Used as a context manager: the process runs until the exit.
    """

    _drop_first = False

    def __init__(self, upstream: tuple[str, int]) -> None:
        self._upstream = upstream
        self._process: subprocess.Popen[str] | None = None
        self.port = 0
        self.dropped_first = 0

    def __enter__(self) -> Self:
        host, port = self._upstream
        command = [sys.executable, str(_RELAY_PROGRAM), host, str(port)]
        if self._drop_first:
            command.append("--drop-first")
        self._process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        return self

    def __exit__(self, *exc_info: object) -> None:
        process, self._process = self._process, None
        assert process is not None
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        if process.returncode:
            raise RuntimeError(f"the relay process exited with {process.returncode}")

    def _command(self, command: str) -> list[str]:
        process = self._process
        assert process is not None
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(f"{command}\n")
        process.stdin.flush()
        answer = process.stdout.readline().split()
        if not answer:
            raise RuntimeError(f"the relay process ended on {command!r} with {process.wait()}")
        return answer

    def open(self) -> None:
        self.port = int(self._command("open")[1])

    def close(self) -> None:
        """Stop listening and close every open socket, as a Redis restart does.

        Records :attr:`dropped_first`, the number of connections the relay closed
        unanswered as its first (1 or 0; always 0 on a relay that forwards its first).
        """
        self.dropped_first = int(self._command("close")[1])


class _DropsFirstHandshake(_Relay):
    """A relay that closes its first accepted connection unanswered, as a proxy with no upstream yet does."""

    _drop_first = True


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
    """Start a worker for an app in a background thread, solo unless the options say otherwise; stopped at teardown.

    The thread runs the worker's hub as the fork gate's engine loop, as the worker runtime runs it.
    """
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

        def run() -> None:
            with liveness_step.hub_as_engine_loop(app):
                worker.start()

        thread = threading.Thread(target=run, name="live-seam-worker", daemon=True)
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
        # A control call that awaits replies over the process-wide pool returns its connection
        # with a reply BRPOP in flight, and a prefork child a later test forks from this process
        # blocks reading it in kombu's after-fork cleanup; the pools are emptied here.
        pools.reset()
        # Hosting a worker in-process arms Celery's "never call get() in a task"
        # guard process-wide; disarm it so later tests can collect results.
        _state._set_task_join_will_block(False)
        celery_settings.cache_clear()


def _result(app: Any, timeout: float) -> int:
    return app.send_task("tests.live_seam_add", args=(2, 3)).get(timeout=timeout, disable_sync_subtasks=False)
