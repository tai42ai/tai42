"""Live verification: the cancel of a running task on a pool that cannot end it or cannot hear the stop.

The ``threads`` pool cannot end a thread, so a worker running the task on it answers the revoke's
stop with an error; ``backend_cancel_task(terminate=True)`` raises that refusal rather than
replying that the task was stopped, and the task runs on to its end and its result. A ``solo``
worker reads no control message while it runs a task, so only the other workers answer the stop,
none of them with the task, and the reply says the stop is not confirmed while the task runs on
to its result. A cancel of a task that has failed sends no revoke, so the task keeps its failure.

Point ``TAI_TEST_CELERY_BROKER`` at a Redis broker to run it (see the live worker harness module).
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from celery.result import AsyncResult
from tai42_kit.backend import UnsupportedBackendTool

from tai42_backend_celery.core import inspection

from ._live_redis_worker import _BROKER, _app_on, _live_marks, _result

_WORKER_PROGRAM = Path(__file__).with_name("_worker_process.py")

pytestmark = _live_marks()


def _is_active(app: Any, task_id: str) -> bool:
    active = app.control.inspect(timeout=1.0).active() or {}
    return any(task["id"] == task_id for tasks in active.values() for task in tasks)


def _until_active(app: Any, task_id: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not _is_active(app, task_id):
        assert time.monotonic() < deadline, f"task {task_id} never ran"


def _until_state(app: Any, task_id: str, state: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while AsyncResult(task_id, app=app).state != state:
        assert time.monotonic() < deadline, f"task {task_id} never reached {state}"
        time.sleep(0.1)


@contextmanager
def _worker_in_its_own_process(app: Any) -> Iterator[None]:
    """A ``solo`` worker of the plugin app in its own process, on a queue no task is sent to; stopped at the exit."""
    hostname = f"apart-{uuid.uuid4().hex}@live"
    queue = f"tai42-live-apart-{uuid.uuid4().hex}"
    process = subprocess.Popen([sys.executable, str(_WORKER_PROGRAM), queue, hostname])
    try:
        deadline = time.monotonic() + 30
        while hostname not in (app.control.inspect(timeout=1.0).ping() or {}):
            assert process.poll() is None, f"the worker process exited with {process.returncode}"
            assert time.monotonic() < deadline, "the worker process never answered a ping"
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise


async def test_a_stop_on_the_threads_pool_raises_the_worker_s_refusal(monkeypatch, run_worker) -> None:
    app = _app_on(monkeypatch, _BROKER)
    monkeypatch.setattr(inspection, "celery_app", app)
    run_worker(app, pool="threads", concurrency=2)
    assert _result(app, timeout=30) == 5
    job = app.send_task("tests.live_async_sleep", args=(3.0,))
    _until_active(app, job.id)

    with pytest.raises(UnsupportedBackendTool) as refused:
        await inspection.CeleryInspection("celery").cancel(job.id, terminate=True)

    assert "backend_cancel_task with terminate on worker 'celery@" in str(refused.value)
    assert "whose pool cannot end a running task: NotImplementedError(" in str(refused.value)
    assert 'does not implement kill_job")' in str(refused.value)
    assert str(refused.value).endswith("; the task is revoked and runs to its end")
    assert _is_active(app, job.id)
    deadline = time.monotonic() + 30
    while _is_active(app, job.id):
        assert time.monotonic() < deadline, f"task {job.id} never ended"
    assert job.get(timeout=5, disable_sync_subtasks=False) == 3.0


async def test_a_stop_while_a_solo_worker_runs_the_task_is_not_confirmed(monkeypatch, run_worker) -> None:
    app = _app_on(monkeypatch, _BROKER)
    monkeypatch.setattr(inspection, "celery_app", app)
    started = threading.Event()

    @app.task(name="tests.live_marked_sleep")
    def marked_sleep(seconds: float) -> float:
        started.set()
        time.sleep(seconds)
        return seconds

    run_worker(app)
    assert _result(app, timeout=30) == 5
    with _worker_in_its_own_process(app):
        job = app.send_task("tests.live_marked_sleep", args=(3.0,))
        assert started.wait(timeout=30), f"task {job.id} never ran"

        reply = await inspection.CeleryInspection("celery").cancel(job.id, terminate=True)

        assert reply == f"Task {job.id} stop requested but not confirmed (status: REVOKED)"
        _until_state(app, job.id, "SUCCESS")
    assert AsyncResult(job.id, app=app).get(timeout=5, disable_sync_subtasks=False) == 3.0


async def test_a_cancel_of_a_failed_task_keeps_its_failure(monkeypatch, run_worker) -> None:
    app = _app_on(monkeypatch, _BROKER)
    monkeypatch.setattr(inspection, "celery_app", app)

    @app.task(name="tests.live_fail")
    def fail() -> None:
        raise ValueError("own failure")

    run_worker(app)
    job = app.send_task("tests.live_fail")
    _until_state(app, job.id, "FAILURE")

    reply = await inspection.CeleryInspection("celery").cancel(job.id, terminate=False)

    assert reply == f"Task {job.id} cannot be canceled (status: FAILURE)"
    with pytest.raises(ValueError, match="own failure"):
        AsyncResult(job.id, app=app).get(timeout=5, disable_sync_subtasks=False)
