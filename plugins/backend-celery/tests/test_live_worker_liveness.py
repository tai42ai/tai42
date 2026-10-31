"""Live verification: a real celery worker reports its progress into the liveness file.

An in-process solo worker on a real Redis broker runs with the progress step registered. Its
file stays fresh while the loop passes; when the broker goes away the loop stops passing and
the file turns stale; once the broker is back, the consumer's restart registers the step's
tick again and the file is fresh again. A job on the solo pool keeps the file fresh from its
own loop up to the job budget. A ``threads`` worker lets it turn stale once one of its jobs runs
past its budget. A job that arrives while a config reload re-imports waits for it, the file
fresh meanwhile. A beat the worker embeds (``-B``) reports into its own file, which the worker
reads, also while it waits for the RedBeat lock another beat holds.

Point ``TAI_TEST_CELERY_BROKER`` at a Redis broker to run it (see the live worker harness module).
"""

from __future__ import annotations

import os
import signal
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import redis
from celery.worker import state
from tai42_kit.fork_gate import fork_gate
from tai42_kit.liveness import LivenessFile, backend_liveness_settings, check_liveness

from tai42_backend_celery.core import embedded_beat, liveness_step

from ._live_redis_worker import _BROKER, _app_on, _live_marks, _Relay, _result

pytestmark = _live_marks()

# A refresh margin and a grace short enough for a stalled loop to show within seconds.
_INTERVAL = 0.2
_GRACE = 0.5


def _until(predicate: Callable[[], bool], what: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, f"{what} never held"
        time.sleep(0.05)


def _alive(path: Path) -> bool:
    return check_liveness(path, _GRACE).alive


def _start(
    monkeypatch: pytest.MonkeyPatch,
    run_worker: Any,
    url: str,
    path: Path,
    *,
    task_timeout: float = 300,
    **worker_options: Any,
) -> Any:
    app = _app_on(monkeypatch, url)
    liveness_step.register(app, LivenessFile(path, _INTERVAL).report, task_timeout=task_timeout)
    run_worker(app, **worker_options)
    return app


def _assert_alive_for(path: Path, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert _alive(path), check_liveness(path, _GRACE).describe()
        time.sleep(0.05)


def test_the_file_stays_fresh_while_the_worker_runs(monkeypatch, run_worker, liveness_path: Path) -> None:
    app = _start(monkeypatch, run_worker, _BROKER, liveness_path)
    assert _result(app, timeout=30) == 5
    _until(lambda: _alive(liveness_path), "a fresh liveness file")

    _assert_alive_for(liveness_path, 3 * (_INTERVAL + 1.0 + _GRACE))


def test_the_file_turns_stale_without_the_broker_and_fresh_after_the_reconnect(
    monkeypatch, run_worker, liveness_path: Path
) -> None:
    upstream = urlsplit(_BROKER)
    with _Relay((upstream.hostname or "localhost", upstream.port or 6379)) as relay:
        relay.open()
        try:
            app = _start(
                monkeypatch, run_worker, f"redis://127.0.0.1:{relay.port}{upstream.path or '/0'}", liveness_path
            )
            assert _result(app, timeout=30) == 5
            _until(lambda: _alive(liveness_path), "a fresh liveness file")

            relay.close()
            _until(lambda: check_liveness(liveness_path, _GRACE).state == "stale", "a stale file during the outage")

            relay.open()
            assert _result(app, timeout=40) == 5
            _until(lambda: _alive(liveness_path), "a fresh file after the reconnect")
        finally:
            relay.close()


def test_a_solo_job_longer_than_the_horizon_keeps_the_file_fresh(monkeypatch, run_worker, liveness_path: Path) -> None:
    app = _start(monkeypatch, run_worker, _BROKER, liveness_path)
    assert _result(app, timeout=30) == 5
    _until(lambda: _alive(liveness_path), "a fresh liveness file")

    job = app.send_task("tests.live_async_sleep", args=(4 * (_INTERVAL + 1.0 + _GRACE),))
    _assert_alive_for(liveness_path, 3 * (_INTERVAL + 1.0 + _GRACE))

    assert job.get(timeout=30, disable_sync_subtasks=False) > 0


def test_a_solo_job_past_its_budget_turns_the_file_stale_until_it_ends(
    monkeypatch, run_worker, liveness_path: Path
) -> None:
    app = _start(monkeypatch, run_worker, _BROKER, liveness_path, task_timeout=1.0)
    assert _result(app, timeout=30) == 5
    _until(lambda: _alive(liveness_path), "a fresh liveness file")

    job = app.send_task("tests.live_async_sleep", args=(6.0,))
    _until(lambda: check_liveness(liveness_path, _GRACE).state == "stale", "a stale file past the job budget")
    assert not job.ready()

    assert job.get(timeout=30, disable_sync_subtasks=False) == 6.0
    _until(lambda: _alive(liveness_path), "a fresh file once the job ended")


def test_a_threads_worker_reads_stale_once_one_job_runs_past_its_budget(
    monkeypatch, run_worker, liveness_path: Path
) -> None:
    app = _start(monkeypatch, run_worker, _BROKER, liveness_path, task_timeout=1.0, pool="threads", concurrency=2)
    assert _result(app, timeout=30) == 5
    _until(lambda: _alive(liveness_path), "a fresh liveness file")

    job = app.send_task("tests.live_async_sleep", args=(6.0,))
    _until(lambda: check_liveness(liveness_path, _GRACE).state == "stale", "a stale file with one job past budget")
    assert not job.ready()

    assert job.get(timeout=30, disable_sync_subtasks=False) == 6.0
    _until(lambda: _alive(liveness_path), "a fresh file once the job ended")


def test_a_job_that_arrives_during_a_reimport_is_deferred_and_the_file_stays_fresh(
    monkeypatch, run_worker, liveness_path: Path
) -> None:
    app = _start(monkeypatch, run_worker, _BROKER, liveness_path)
    assert _result(app, timeout=30) == 5
    _until(lambda: _alive(liveness_path), "a fresh liveness file")
    held, release = threading.Event(), threading.Event()

    def _reimport() -> None:
        with fork_gate.exclusive(timeout=5.0):
            held.set()
            release.wait(30.0)

    rebuild = threading.Thread(target=_reimport, daemon=True)
    rebuild.start()
    try:
        assert held.wait(5.0)
        job = app.send_task("tests.live_async_sleep", args=(0.0,))
        _assert_alive_for(liveness_path, 3 * (_INTERVAL + 1.0 + _GRACE))
        assert not job.ready(), "the job ran while the re-import held the fork gate"
    finally:
        release.set()
        rebuild.join(10.0)

    assert job.get(timeout=30, disable_sync_subtasks=False) == 0.0


def test_an_embedded_beat_reports_into_its_own_file_which_the_worker_reads(
    monkeypatch, run_worker, liveness_path: Path
) -> None:
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_INTERVAL", str(_INTERVAL))
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_GRACE", str(_GRACE))
    backend_liveness_settings.cache_clear()
    settings = backend_liveness_settings()
    beat_path = embedded_beat.embedded_beat_path(settings)
    app = _app_on(monkeypatch, _BROKER)
    embedded_beat.register(app, settings)
    app.conf.redbeat_redis_url = _BROKER
    app.conf.redbeat_key_prefix = f"tai42-live-redbeat-{uuid.uuid4().hex}:"
    # RedBeat sleeps at most this long between ticks with nothing scheduled, so it is the beat's promise.
    app.conf.beat_max_loop_interval = 1
    liveness_step.register(app, LivenessFile(liveness_path, _INTERVAL).report, task_timeout=300)
    worker = run_worker(app, beat=True)
    _until(lambda: worker.beat is not None and check_liveness(beat_path, _GRACE).alive, "a fresh beat file")
    _until(lambda: _alive(liveness_path), "a fresh worker file")

    beat_pid = check_liveness(beat_path, _GRACE).pid
    assert beat_pid is not None
    assert beat_pid == worker.beat.pid
    assert beat_pid != os.getpid()
    assert check_liveness(liveness_path, _GRACE).pid == os.getpid()

    os.kill(beat_pid, signal.SIGSTOP)
    try:
        _until(lambda: check_liveness(liveness_path, _GRACE).state == "stale", "a stale worker file, its beat stopped")
    finally:
        os.kill(beat_pid, signal.SIGCONT)
    _until(lambda: _alive(liveness_path), "a fresh worker file once its beat ticks again")

    state.should_stop = 0
    _until(lambda: not worker.beat.is_alive(), "the embedded beat ending with its worker")
    embedded_beat.release()
    assert not beat_path.exists()


def test_a_second_beat_waiting_on_the_lock_keeps_its_file_fresh(monkeypatch, run_worker, liveness_path: Path) -> None:
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_INTERVAL", str(_INTERVAL))
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_GRACE", str(_GRACE))
    backend_liveness_settings.cache_clear()
    settings = backend_liveness_settings()
    beat_path = embedded_beat.embedded_beat_path(settings)
    app = _app_on(monkeypatch, _BROKER)
    embedded_beat.register(app, settings)
    prefix = f"tai42-live-redbeat-{uuid.uuid4().hex}:"
    lock_key = f"{prefix}lock"
    app.conf.redbeat_redis_url = _BROKER
    app.conf.redbeat_key_prefix = prefix
    app.conf.redbeat_lock_key = lock_key
    app.conf.beat_max_loop_interval = 1
    client = redis.Redis.from_url(_BROKER)
    client.set(lock_key, "another beat", ex=300)
    try:
        liveness_step.register(app, LivenessFile(liveness_path, _INTERVAL).report, task_timeout=300)
        worker = run_worker(app, beat=True)
        _until(lambda: worker.beat is not None and check_liveness(beat_path, _GRACE).alive, "a fresh beat file")

        _assert_alive_for(beat_path, 3.0)
        _until(lambda: _alive(liveness_path), "a fresh worker file while its beat waits for the lock")
        assert client.get(lock_key) == b"another beat"

        client.delete(lock_key)
        _until(lambda: client.get(lock_key) not in (None, b"another beat"), "the waiting beat taking the lock")
        _assert_alive_for(beat_path, 2.0)

        state.should_stop = 0
        _until(lambda: not worker.beat.is_alive(), "the embedded beat ending with its worker")
        embedded_beat.release()
    finally:
        client.delete(lock_key)
        client.close()


def test_a_worker_stopped_before_its_consumer_starts_reaps_its_embedded_beat_and_removes_its_file(
    monkeypatch, run_worker, liveness_path: Path
) -> None:
    """With the broker down at boot the consumer never starts, while the embedded beat already ticks.

    The broker is a listener that accepts connections and never answers, so the consumer's first
    connect waits on it until the test drops it. The beat's first tick and the consumer giving up
    are independent, so the drop comes once the beat has ticked: the worker then stops on its
    failed connect with a beat file in place.
    """
    import socket
    import threading

    monkeypatch.setenv("TAI_BACKEND_LIVENESS_INTERVAL", str(_INTERVAL))
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_GRACE", str(_GRACE))
    backend_liveness_settings.cache_clear()
    settings = backend_liveness_settings()
    beat_path = embedded_beat.embedded_beat_path(settings)
    silent = socket.create_server(("127.0.0.1", 0))
    held: list[socket.socket] = []

    def hold() -> None:
        while True:
            try:
                held.append(silent.accept()[0])
            except OSError:
                return

    holder = threading.Thread(target=hold, daemon=True)
    holder.start()
    try:
        app = _app_on(monkeypatch, f"redis://127.0.0.1:{silent.getsockname()[1]}/0")
        embedded_beat.register(app, settings)
        app.conf.redbeat_redis_url = _BROKER
        app.conf.redbeat_key_prefix = f"tai42-live-redbeat-{uuid.uuid4().hex}:"
        app.conf.beat_max_loop_interval = 1
        liveness_step.register(app, LivenessFile(liveness_path, _INTERVAL).report, task_timeout=300)
        worker = run_worker(app, beat=True)
        _until(lambda: worker.beat is not None and check_liveness(beat_path, _GRACE).alive, "a fresh beat file")
        assert not liveness_path.exists(), "the consumer started against a broker that is down"

        state.should_stop = 0
    finally:
        # Shutting the listener down wakes the blocked accept, so every held connection is listed.
        silent.shutdown(socket.SHUT_RDWR)
        silent.close()
        holder.join(timeout=5)
        assert not holder.is_alive(), "the silent broker's accept loop did not end"
        for connection in held:
            connection.close()
    _until(
        lambda: not any(thread.name == "live-seam-worker" for thread in threading.enumerate()), "the worker stopping"
    )
    embedded_beat.release()

    assert not beat_path.exists(), check_liveness(beat_path, _GRACE).describe()
