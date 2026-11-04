"""The inspection against a real Redis and a real RQ worker (``prefork``, and ``solo`` for the
stop): both forms of ``backend_cancel_task`` on a queued job and on a running one, and the running
job ``backend_active_tasks`` lists.

The worker serves a queue of its own per test, so no other job reaches it. Skipped
when no Redis is reachable (see ``needs_live_redis`` in the conftest).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, cast

import pytest
from redis import Redis
from rq import Queue
from rq.job import Job, JobStatus

from tai42_backend_rq.backend import RqBackend
from tai42_backend_rq.settings import rq_settings

from .conftest import LIVE_REDIS_URL, backend_tools, needs_live_redis

pytestmark = needs_live_redis

tools = backend_tools()


def hold(marker_key: str, seconds: float) -> str:
    """RQ job body: mark its start, run ``seconds`` in short sleeps, mark its end.

    The short sleeps let a stop raised into the job's thread (the ``solo`` pool's) land between them.
    """
    conn = Redis.from_url(LIVE_REDIS_URL)
    try:
        conn.rpush(marker_key, "started")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.05)
        conn.rpush(marker_key, "finished")
    finally:
        conn.close()
    return "ok"


@pytest.fixture
async def queue(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Queue]:
    """A queue of this test's own, served by a real worker running in this process.

    The worker runs on the pool the test names, ``prefork`` unless it names one.
    """
    pool = getattr(request, "param", "prefork")
    name = f"cancel-live-{uuid.uuid4().hex}"
    monkeypatch.setenv("RQ_REDIS_URL", LIVE_REDIS_URL)
    monkeypatch.setenv("RQ_QUEUE_NAME", name)
    rq_settings.cache_clear()
    conn = Redis.from_url(LIVE_REDIS_URL)
    backend = cast("Any", RqBackend)()
    worker_task = asyncio.create_task(
        backend.launch(["worker", "--redis-url", LIVE_REDIS_URL, "--name", name, "--loglevel", "ERROR", "--pool", pool])
    )
    try:
        yield Queue(name, connection=conn)
    finally:
        worker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await worker_task
        conn.close()
        rq_settings.cache_clear()


async def _until(predicate: Any, timeout: float = 30.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.05)


def _marks(conn: Redis, marker_key: str) -> list[str]:
    return [v.decode() for v in cast("list[bytes]", conn.lrange(marker_key, 0, -1))]


def _enqueue(queue: Queue, seconds: float) -> tuple[Job, str]:
    marker_key = f"cancel-live-marks:{uuid.uuid4().hex}"
    return queue.enqueue(hold, marker_key, seconds), marker_key


async def _running(queue: Queue, seconds: float) -> tuple[Job, str]:
    job, marker_key = _enqueue(queue, seconds)
    await _until(lambda: _marks(queue.connection, marker_key) == ["started"])
    return job, marker_key


async def test_cancel_without_terminate_leaves_a_running_job_and_its_data_alone(queue: Queue) -> None:
    job, marker_key = await _running(queue, 2.0)

    assert await tools.backend_cancel_task(job.id) == f"Task {job.id} is running and is left to finish"
    assert await tools.backend_task_status(job.id) == JobStatus.STARTED
    assert Job.fetch(job.id, connection=queue.connection).func_name == f"{hold.__module__}.hold"

    await _until(lambda: job.get_status() == JobStatus.FINISHED)
    assert _marks(queue.connection, marker_key) == ["started", "finished"]
    assert job.return_value() == "ok"


@pytest.mark.parametrize("terminate", [False, True])
async def test_cancel_keeps_a_queued_job_from_running(queue: Queue, terminate: bool) -> None:
    ahead, _ = await _running(queue, 1.0)
    job, marker_key = _enqueue(queue, 0.0)

    assert await tools.backend_cancel_task(job.id, terminate=terminate) == f"Task {job.id} canceled"

    await _until(lambda: ahead.get_status() == JobStatus.FINISHED)
    # The worker would take the next job at once; give it the time to.
    await asyncio.sleep(1.0)
    assert job.get_status() == JobStatus.CANCELED
    assert _marks(queue.connection, marker_key) == []


@pytest.mark.parametrize("queue", ["prefork", "solo"], indirect=True)
async def test_cancel_with_terminate_stops_a_running_job(queue: Queue) -> None:
    job, marker_key = await _running(queue, 30.0)

    assert await tools.backend_cancel_task(job.id, terminate=True) == f"Task {job.id} stopped"
    assert job.get_status() == JobStatus.STOPPED
    assert job.id in queue.failed_job_registry
    assert _marks(queue.connection, marker_key) == ["started"]


async def test_active_tasks_lists_a_running_job_from_rqs_job_record(queue: Queue) -> None:
    job, _ = await _running(queue, 2.0)
    running = Job.fetch(job.id, connection=queue.connection)
    assert running.worker_name is not None
    assert running.enqueued_at is not None
    assert running.started_at is not None

    assert await tools.backend_active_tasks() == {
        running.worker_name: [
            {
                "id": job.id,
                "description": running.description,
                "origin": queue.name,
                "status": "started",
                "enqueued_at": running.enqueued_at.isoformat(),
                "started_at": running.started_at.isoformat(),
            }
        ]
    }
    assert await tools.backend_active_tasks(running.worker_name) == await tools.backend_active_tasks()
    await _until(lambda: job.get_status() == JobStatus.FINISHED)
