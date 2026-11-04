"""The gevent pool's worker runs rq's scheduler as a thread of its own process.

rq forks its scheduler as a child; the gevent worker starts it on a thread of the worker process
instead, so the scheduler registers this process's pid, holds the queue's scheduler lock from
here, and a stop joins the thread, releasing the lock and deleting the scheduler's key. A thread
that dies is restarted by the worker's maintenance pass.

The restart case runs against a real Redis and is skipped when none is reachable — set
``TAI_TEST_RQ_REDIS`` (default ``redis://localhost:6379/15``) to run it.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import fakeredis
import pytest
from redis import Redis
from rq import Queue
from rq.scheduler import RQScheduler

from tai42_backend_rq import worker_pools
from tai42_backend_rq.scheduler_thread import ThreadedScheduler

_REDIS_URL = os.environ.get("TAI_TEST_RQ_REDIS", "redis://localhost:6379/15")


def _redis_reachable(url: str) -> bool:
    try:
        Redis.from_url(url).ping()
    except Exception:
        return False
    return True


def _no_report(*, next_within: float) -> None:
    """A progress reporter that drops every report."""


def _gevent_worker() -> tuple[worker_pools.CustomRQGeventWorker, Queue, fakeredis.FakeStrictRedis]:
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    queue = Queue("q", connection=conn)
    return worker_pools.CustomRQGeventWorker([queue], connection=conn, report_progress=_no_report), queue, conn


def _scheduler_thread(scheduler: RQScheduler | None) -> threading.Thread:
    """The thread ``scheduler`` runs on, failing the test at once when it runs as anything else.

    A scheduler forked as a child never sees the stop flag set in this process, so a stop's join, and
    the interpreter's exit-time join of its children, would wait on it for ever; such a child is
    killed before the test fails.
    """
    runner = None if scheduler is None else scheduler._process
    if isinstance(runner, threading.Thread):
        return runner
    if runner is not None:
        runner.kill()
        runner.join()
    pytest.fail(f"the scheduler runs as {type(runner).__name__}, not a thread of this process")


def _wait_for(predicate: Callable[[], object], *, within: float = 5.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_the_gevent_worker_runs_rq_s_scheduler_on_a_thread_of_its_own_process() -> None:
    worker, _, conn = _gevent_worker()

    worker._start_scheduler(False, "INFO")
    scheduler = worker.scheduler
    thread = _scheduler_thread(scheduler)
    assert isinstance(scheduler, ThreadedScheduler)
    try:
        assert thread.is_alive()
        assert _wait_for(lambda: conn.exists(scheduler.key)), "the scheduler never registered its birth"
        assert int(conn.hget(scheduler.key, "pid")) == os.getpid()  # pyright: ignore[reportArgumentType]
        assert conn.get(ThreadedScheduler.get_locking_key("q")) == scheduler.name.encode()
    finally:
        worker.stop_scheduler()

    assert not thread.is_alive()
    assert not conn.exists(ThreadedScheduler.get_locking_key("q"))
    assert not conn.exists(scheduler.key)


def test_a_burst_gevent_worker_enqueues_the_due_jobs_and_starts_no_thread() -> None:
    worker, queue, conn = _gevent_worker()
    job = queue.enqueue_at(datetime.now(UTC) - timedelta(seconds=1), "builtins.len", "x")

    worker._start_scheduler(True, "INFO")

    scheduler = worker.scheduler
    assert isinstance(scheduler, ThreadedScheduler)
    assert scheduler._process is None
    assert queue.get_job_ids() == [job.id]
    assert not conn.exists(ThreadedScheduler.get_locking_key("q"))
    assert not conn.exists(scheduler.key)


@pytest.mark.skipif(not _redis_reachable(_REDIS_URL), reason=f"no Redis reachable at {_REDIS_URL}")
def test_the_maintenance_pass_restarts_a_scheduler_thread_that_died(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = Redis.from_url(_REDIS_URL)
    queue = Queue(f"scheduler-restart-{uuid.uuid4().hex}", connection=conn)
    lock_key = ThreadedScheduler.get_locking_key(queue.name)
    worker = worker_pools.CustomRQGeventWorker([queue], connection=conn, report_progress=_no_report)
    crashes: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda hook_args: crashes.append(hook_args.exc_value))

    worker._start_scheduler(False, "INFO")
    scheduler = worker.scheduler
    first = _scheduler_thread(scheduler)
    assert isinstance(scheduler, ThreadedScheduler)
    try:
        enqueue_scheduled_jobs = scheduler.enqueue_scheduled_jobs

        def crash_once() -> None:
            if not crashes:
                raise RuntimeError("scheduler pass failed")
            enqueue_scheduled_jobs()

        monkeypatch.setattr(scheduler, "enqueue_scheduled_jobs", crash_once)
        assert _wait_for(lambda: not first.is_alive()), "the scheduler thread never died"
        assert [str(exc) for exc in crashes] == ["scheduler pass failed"]
        assert conn.get(lock_key) == scheduler.name.encode()

        worker.last_cleaned_at = datetime.now(UTC) - timedelta(hours=1)
        worker.run_maintenance_tasks()

        second = _scheduler_thread(scheduler)
        assert second is not first
        assert second.is_alive(), "the maintenance pass did not restart the scheduler thread"
        job = queue.enqueue_in(timedelta(seconds=1), "builtins.len", "x")
        assert _wait_for(lambda: job.id in queue.get_job_ids()), "the restarted scheduler never moved the delayed job"
    finally:
        worker.stop_scheduler()
        queue.empty()

    assert not second.is_alive()
    assert not conn.exists(lock_key)
    assert not conn.exists(scheduler.key)
