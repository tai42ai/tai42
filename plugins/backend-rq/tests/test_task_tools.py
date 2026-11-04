"""The RQ task and worker tools over its worker inspection: ping, task status and result across
states, cancel, and the queue and worker statistics.
"""

from __future__ import annotations

import asyncio
import base64
import pickle
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rq import Queue
from rq.exceptions import InvalidJobOperation
from rq.job import Job, JobStatus
from rq.registry import CanceledJobRegistry, ScheduledJobRegistry

from tai42_backend_rq import inspection
from tai42_backend_rq.schedules import scheduler_for
from tai42_backend_rq.settings import rq_settings

from .conftest import (
    FakeAsyncRedis,
    _patch_redis,
    backend_tools,
)

tools = backend_tools()


def _fresh_heartbeat() -> str:
    return datetime.now(UTC).isoformat()


async def test_ping_worker_specific_and_all(monkeypatch):
    settings = rq_settings()
    # The registry set stores full worker KEYS; the hashes live under them.
    redis = FakeAsyncRedis(
        hashes={
            settings.rq_worker_key("w1"): {"last_heartbeat": _fresh_heartbeat().encode()},
            settings.rq_worker_key("w2"): {"last_heartbeat": b"2000-01-01T00:00:00Z"},
        },
        sets={settings.rq_workers_key: {b"rq:worker:w1", b"rq:worker:w2"}},
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_ping_worker("w1") == {"w1": "pong"}
    # Stale heartbeat -> not alive.
    all_ping = await tools.backend_ping_worker(None)
    assert all_ping == {"w1": "pong"}
    assert await tools.backend_list_active_workers() == ["w1"]


async def test_task_status(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(hashes={settings.rq_job_key("t1"): {"status": b"queued"}})
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_task_status("t1") == "queued"
    assert await tools.backend_task_status("missing") == "unknown"


def _finished_result_redis(task_id: str, return_value: Any) -> FakeAsyncRedis:
    settings = rq_settings()
    encoded = base64.b64encode(pickle.dumps(return_value))
    return FakeAsyncRedis(
        hashes={settings.rq_job_key(task_id): {"status": JobStatus.FINISHED.encode()}},
        streams={settings.rq_result_key(task_id): [(b"0-1", {b"return_value": encoded})]},
    )


async def test_task_result_returns_decoded_value(monkeypatch):
    """A well-formed stored result decodes to its original value."""
    _patch_redis(monkeypatch, _finished_result_redis("task-ok", {"answer": 42}))
    assert await tools.backend_task_result("task-ok") == {"answer": 42}


async def test_task_result_returns_the_latest_run(monkeypatch):
    """A recurring job re-uses its id, appending one stream entry per run; the
    NEWEST entry is the task's result."""
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key("t-multi"): {"status": JobStatus.FINISHED.encode()}},
        streams={
            settings.rq_result_key("t-multi"): [
                (b"0-1", {b"return_value": base64.b64encode(pickle.dumps("old"))}),
                (b"0-2", {b"return_value": base64.b64encode(pickle.dumps("new"))}),
            ]
        },
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_task_result("t-multi") == "new"


async def test_task_result_raises_on_corrupt_result(monkeypatch):
    """A corrupt stored result raises loudly instead of returning a string."""
    settings = rq_settings()
    task_id = "task-corrupt"
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key(task_id): {"status": JobStatus.FINISHED.encode()}},
        streams={settings.rq_result_key(task_id): [(b"0-1", {b"return_value": b"!!!not-base64-or-pickle!!!"})]},
    )
    _patch_redis(monkeypatch, redis)

    with pytest.raises(ValueError, match=f"Failed to decode result for task {task_id}"):
        await tools.backend_task_result(task_id)


async def test_task_result_states(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={
            settings.rq_job_key("t-stopped"): {"status": JobStatus.STOPPED.encode()},
            settings.rq_job_key("t-started"): {"status": JobStatus.STARTED.encode()},
            settings.rq_job_key("t-empty"): {"status": JobStatus.FINISHED.encode()},
        },
    )
    _patch_redis(monkeypatch, redis)

    assert "not found" in await tools.backend_task_result("missing")
    assert "did not complete" in await tools.backend_task_result("t-stopped")
    # No timeout: snapshot without waiting.
    assert "is not ready" in await tools.backend_task_result("t-started")
    # Finished but no stream entry.
    assert "No result found" in await tools.backend_task_result("t-empty")


async def test_task_result_failed_raises_the_stored_failure(monkeypatch):
    """A FAILED task raises the persisted traceback text — never an error
    string return."""
    settings = rq_settings()
    exc_string = base64.b64encode(zlib.compress(b"Traceback ...\nValueError: boom"))
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key("t-failed"): {"status": JobStatus.FAILED.encode()}},
        streams={settings.rq_result_key("t-failed"): [(b"0-1", {b"exc_string": exc_string})]},
    )
    _patch_redis(monkeypatch, redis)

    with pytest.raises(RuntimeError, match="ValueError: boom"):
        await tools.backend_task_result("t-failed")


async def test_task_result_failed_without_details_still_raises(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key("t-failed"): {"status": JobStatus.FAILED.encode()}},
    )
    _patch_redis(monkeypatch, redis)

    with pytest.raises(RuntimeError, match="no failure details retained"):
        await tools.backend_task_result("t-failed")


async def test_task_result_finished_with_empty_payload_is_none(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={settings.rq_job_key("t-none"): {"status": JobStatus.FINISHED.encode()}},
        streams={settings.rq_result_key("t-none"): [(b"0-1", {})]},
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_task_result("t-none") is None


async def test_task_result_polls_until_finished(monkeypatch):
    """With a timeout, a running task is polled until it finishes."""
    settings = rq_settings()
    redis = _finished_result_redis("t-poll", "done")
    statuses = iter([JobStatus.STARTED.encode(), JobStatus.FINISHED.encode()])
    real_hget = redis.hget

    async def hget(key: str, field: str) -> Any:
        if key == settings.rq_job_key("t-poll") and field == "status":
            return next(statuses)
        return await real_hget(key, field)

    redis.hget = hget  # type: ignore[method-assign]
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_task_result("t-poll", timeout=5) == "done"


async def test_task_result_timeout_elapses(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(hashes={settings.rq_job_key("t-slow"): {"status": JobStatus.STARTED.encode()}})
    _patch_redis(monkeypatch, redis)

    assert "is not ready" in await tools.backend_task_result("t-slow", timeout=0.3)


async def test_reserved_scheduled_and_queues(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={
            settings.rq_worker_key("w1"): {
                "queues": b'["default","high"]',
                "state": b"busy",
            },
            settings.rq_worker_key("w2"): {"queues": b"default,low"},
        },
        sets={
            # Both registry sets store full KEYS ("rq:worker:<name>" /
            # "rq:queue:<name>"), never bare names.
            settings.rq_workers_key: {b"rq:worker:w1", b"rq:worker:w2"},
            settings.rq_queues_key: {b"rq:queue:default"},
        },
        lists={settings.rq_queue_key("default"): [b"j2", b"j3"]},
        zsets={
            # Recurring schedules and ETA/countdown jobs live in separate
            # zsets; the scheduled-tasks read merges both.
            settings.rq_scheduler_zset: {"sched-1": 123.0},
            settings.rq_scheduled_registry_key("default"): {"eta-1": 456.0},
        },
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_reserved_tasks() == {"default": ["j2", "j3"]}
    assert await tools.backend_scheduled_tasks() == {
        "sched-1": {"next_run_at_ts": 123.0},
        "eta-1": {"next_run_at_ts": 456.0},
    }

    queues = await tools.backend_worker_queues()
    assert queues == {"w1": ["default", "high"], "w2": ["default", "low"]}
    assert await tools.backend_worker_queues("w2") == {"w2": ["default", "low"]}


async def test_worker_stats(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={settings.rq_worker_key("w1"): {b"state": b"busy", b"successful_job_count": b"7"}},
        sets={settings.rq_workers_key: {b"rq:worker:w1"}},
    )
    _patch_redis(monkeypatch, redis)

    assert await tools.backend_worker_stats() == {"w1": {"state": "busy", "successful_job_count": "7"}}
    assert await tools.backend_worker_stats("w1") == {"w1": {"state": "busy", "successful_job_count": "7"}}


# -- cancel: rq's own job structures over one in-memory server ----------------------


def _left_to_finish(job_id: str) -> str:
    return f"Task {job_id} is running and is left to finish"


def _queue(conn: Any) -> Queue:
    return Queue(rq_settings().queue_name, connection=conn)


def _enqueue(conn: Any) -> Job:
    return _queue(conn).enqueue("os.getpid")


def _claim(conn: Any, job: Job) -> None:
    """What a worker does on dequeue: move the id off the queue list, the job still reading queued."""
    queue = _queue(conn)
    conn.lmove(queue.key, queue.intermediate_queue_key)


def _start(conn: Any, job: Job, worker: str = "w1") -> None:
    """What a worker does once it has claimed the job: mark it started under its name."""
    _claim(conn, job)
    with conn.pipeline() as pipe:
        job.prepare_for_execution(worker, pipeline=pipe)
        pipe.execute()


def _status(conn: Any, job_id: str) -> str | None:
    raw = conn.hget(rq_settings().rq_job_key(job_id), "status")
    return None if raw is None else raw.decode()


def _canceled(conn: Any, job: Job) -> bool:
    return _status(conn, job.id) == JobStatus.CANCELED and job.id in CanceledJobRegistry(job.origin, connection=conn)


def _busy(conn: Any, worker: str, job: Job) -> None:
    """What a registered worker running ``job`` holds: its registry entry naming the job."""
    settings = rq_settings()
    conn.sadd(settings.rq_workers_key, settings.rq_worker_key(worker))
    conn.hset(settings.rq_worker_key(worker), "current_job", job.id)


async def test_active_tasks_lists_each_workers_running_job_from_its_record(rq_server):
    job = _enqueue(rq_server)
    _start(rq_server, job)
    _busy(rq_server, "w1", job)
    rq_server.sadd(rq_settings().rq_workers_key, rq_settings().rq_worker_key("w2"))
    running = Job.fetch(job.id, connection=rq_server)
    assert running.enqueued_at is not None
    assert running.started_at is not None
    listed = {
        "w1": [
            {
                "id": job.id,
                "description": "os.getpid()",
                "origin": rq_settings().queue_name,
                "status": "started",
                "enqueued_at": running.enqueued_at.isoformat(),
                "started_at": running.started_at.isoformat(),
            }
        ]
    }

    assert await tools.backend_active_tasks() == listed
    assert await tools.backend_active_tasks("w1") == listed
    assert await tools.backend_active_tasks("w2") == {}


async def test_active_tasks_leaves_out_a_worker_whose_job_has_gone(rq_server):
    job = _enqueue(rq_server)
    _start(rq_server, job)
    _busy(rq_server, "w1", job)
    rq_server.delete(job.key)

    assert await tools.backend_active_tasks() == {}


async def test_active_tasks_leaves_out_a_worker_whose_job_has_ended(rq_server):
    """RQ keeps an ended job's record (``--results-ttl``); a worker's job that ends between the two
    reads is not running, so it is not listed."""
    job = _enqueue(rq_server)
    _start(rq_server, job)
    _busy(rq_server, "w1", job)
    job.set_status(JobStatus.FINISHED)

    assert await tools.backend_active_tasks() == {}


@pytest.fixture
def stops(monkeypatch, rq_server) -> list[str]:
    """Each job id a stop-job command is sent for; the command stops the job as a forking worker does."""
    sent: list[str] = []

    def _send(conn: Any, job_id: str, serializer: Any = None) -> None:
        job = Job.fetch(job_id, connection=conn)
        if not job.worker_name:
            raise InvalidJobOperation("Job is not currently executing")
        sent.append(job_id)
        job.set_status(JobStatus.STOPPED)

    monkeypatch.setattr(inspection, "send_stop_job_command", _send)
    return sent


async def test_the_inspection_addresses_workers_and_terminates():
    view = inspection.RqInspection("rq")
    assert view.addresses_workers is True
    assert view.terminates is True


@pytest.mark.parametrize("terminate", [False, True])
async def test_cancel_task_cancels_a_queued_job_with_rqs_own_cancel(rq_server, terminate):
    """A job no worker has claimed is cancelled with or without ``terminate``: off its queue, kept
    with its data and the ``canceled`` status in the canceled registry."""
    job = _enqueue(rq_server)

    assert await tools.backend_cancel_task(job.id, terminate=terminate) == f"Task {job.id} canceled"
    assert _canceled(rq_server, job)
    assert job.id not in _queue(rq_server).job_ids
    assert Job.fetch(job.id, connection=rq_server).func_name == "os.getpid"


async def test_cancel_task_cancels_a_job_waiting_for_its_time(rq_server):
    job = _queue(rq_server).enqueue_in(timedelta(minutes=5), "os.getpid")

    assert await tools.backend_cancel_task(job.id) == f"Task {job.id} canceled"
    assert _canceled(rq_server, job)
    assert job.id not in ScheduledJobRegistry(job.origin, connection=rq_server)


async def test_cancel_task_without_terminate_leaves_a_running_job_to_finish(rq_server):
    """A cancel without ``terminate`` leaves a running job and its record as they are: RQ's
    ``Job.cancel`` would rewrite a running job's record, so it is not called."""
    job = _enqueue(rq_server)
    _start(rq_server, job)
    record = rq_server.hgetall(job.key)

    assert await tools.backend_cancel_task(job.id) == _left_to_finish(job.id)
    assert rq_server.hgetall(job.key) == record
    assert job.id not in CanceledJobRegistry(job.origin, connection=rq_server)


async def test_cancel_task_without_terminate_leaves_a_job_a_worker_has_claimed_to_finish(rq_server):
    """A job a worker has taken off its queue still reads ``queued`` until it starts; rq's worker
    runs it whatever its status says, so it is left to finish like a running one."""
    job = _enqueue(rq_server)
    _claim(rq_server, job)
    record = rq_server.hgetall(job.key)

    assert await tools.backend_cancel_task(job.id) == _left_to_finish(job.id)
    assert rq_server.hgetall(job.key) == record
    assert rq_server.lrange(_queue(rq_server).intermediate_queue_key, 0, -1) == [job.id.encode()]


async def test_cancel_task_never_cancels_a_job_a_worker_claims_meanwhile(monkeypatch, rq_server):
    """A worker claiming the job between the queue check and the cancel moves it off the queue list
    the cancel watches, so the cancel is never written: the retry sees the claim and leaves the job
    to finish."""
    job = _enqueue(rq_server)
    claims: list[str] = []
    pipeline = rq_server.pipeline

    def _pipeline(*args: Any, **kwargs: Any) -> Any:
        pipe = pipeline(*args, **kwargs)
        lpos = pipe.lpos

        def _lpos_then_claim(*lpos_args: Any) -> Any:
            found = lpos(*lpos_args)
            if pipe.watching and not claims:
                claims.append(job.id)
                _claim(rq_server, job)
            return found

        pipe.lpos = _lpos_then_claim
        return pipe

    monkeypatch.setattr(rq_server, "pipeline", _pipeline)

    assert await tools.backend_cancel_task(job.id) == _left_to_finish(job.id)
    assert claims == [job.id]
    assert _status(rq_server, job.id) == JobStatus.QUEUED


def _before_the_cancel_is_queued(monkeypatch: pytest.MonkeyPatch, conn: Any, change: Any) -> list[bool]:
    """Run ``change`` once, between the cancel's watched read and its queued ``Job.cancel``.

    Returns a list that holds ``True`` once ``change`` has run.
    """
    ran: list[bool] = []
    pipeline = conn.pipeline

    def _pipeline(*args: Any, **kwargs: Any) -> Any:
        pipe = pipeline(*args, **kwargs)
        multi = pipe.multi

        def _change_then_multi() -> None:
            if pipe.watching and not ran:
                ran.append(True)
                change()
            multi()

        pipe.multi = _change_then_multi
        return pipe

    monkeypatch.setattr(conn, "pipeline", _pipeline)
    return ran


@pytest.mark.parametrize("terminate", [False, True])
async def test_cancel_task_reports_a_job_another_cancel_takes_meanwhile(monkeypatch, rq_server, terminate):
    """Two cancels of one job: the one that loses the race reads the job the other cancelled."""
    job = _enqueue(rq_server)
    ran = _before_the_cancel_is_queued(monkeypatch, rq_server, Job.fetch(job.id, connection=rq_server).cancel)

    out = await tools.backend_cancel_task(job.id, terminate=terminate)
    assert out == f"Task {job.id} cannot be canceled (status: canceled)"
    assert ran == [True]
    assert _canceled(rq_server, job)


async def test_cancel_task_reports_a_job_whose_record_expires_meanwhile(monkeypatch, rq_server):
    """A job whose record goes (its ttl ends) between the cancel's read and its cancel is not found."""
    job = _enqueue(rq_server)
    ran = _before_the_cancel_is_queued(monkeypatch, rq_server, lambda: rq_server.delete(job.key))

    assert await tools.backend_cancel_task(job.id) == f"Task {job.id} not found"
    assert ran == [True]


async def test_cancel_task_never_cancels_a_job_whose_record_changes_meanwhile(monkeypatch, rq_server):
    """The cancel watches the job's record: a record that ends meanwhile, its queue list untouched,
    fails the transaction, and the retry reads the ended job."""
    job = _enqueue(rq_server)
    ran = _before_the_cancel_is_queued(
        monkeypatch, rq_server, lambda: rq_server.hset(job.key, "status", JobStatus.FINISHED.value)
    )

    out = await tools.backend_cancel_task(job.id)
    assert out == f"Task {job.id} cannot be canceled (status: finished)"
    assert ran == [True]
    assert _status(rq_server, job.id) == JobStatus.FINISHED


@pytest.mark.parametrize("terminate", [False, True])
async def test_cancel_task_reports_an_ended_job(rq_server, terminate):
    job = _enqueue(rq_server)
    _start(rq_server, job)
    job.set_status(JobStatus.FINISHED)

    out = await tools.backend_cancel_task(job.id, terminate=terminate)
    assert out == f"Task {job.id} cannot be canceled (status: finished)"
    assert _status(rq_server, job.id) == JobStatus.FINISHED


@pytest.mark.parametrize("terminate", [False, True])
async def test_cancel_task_reports_a_job_rq_does_not_hold(rq_server, terminate):
    assert await tools.backend_cancel_task("j1", terminate=terminate) == "Task j1 not found"


async def test_cancel_task_with_terminate_stops_a_running_job(rq_server, stops):
    job = _enqueue(rq_server)
    _start(rq_server, job)

    assert await tools.backend_cancel_task(job.id, terminate=True) == f"Task {job.id} stopped"
    assert stops == [job.id]


async def test_cancel_task_with_terminate_stops_a_claimed_job_once_it_starts(rq_server, stops):
    job = _enqueue(rq_server)
    _claim(rq_server, job)

    async def _starts_later() -> None:
        await asyncio.sleep(0.3)
        with rq_server.pipeline() as pipe:
            job.prepare_for_execution("w1", pipeline=pipe)
            pipe.execute()

    starting = asyncio.create_task(_starts_later())
    assert await tools.backend_cancel_task(job.id, terminate=True) == f"Task {job.id} stopped"
    await starting
    assert stops == [job.id]


async def test_cancel_task_with_terminate_reports_a_job_ending_before_the_stop(monkeypatch, rq_server, stops):
    """A job that ends between the read and the stop leaves rq's stop-job command nothing to stop
    (it refuses a job no worker runs); the reply names the status the job ended in."""
    job = _enqueue(rq_server)
    _start(rq_server, job)
    send = inspection.send_stop_job_command

    def _finishes_first(conn: Any, job_id: str, serializer: Any = None) -> None:
        with conn.pipeline() as pipe:
            job.set_status(JobStatus.FINISHED, pipeline=pipe)
            pipe.hset(job.key, "worker_name", "")
            pipe.execute()
        send(conn, job_id)

    monkeypatch.setattr(inspection, "send_stop_job_command", _finishes_first)

    out = await tools.backend_cancel_task(job.id, terminate=True)
    assert out == f"Task {job.id} cannot be stopped (status: finished)"
    assert stops == []


async def test_cancel_task_reports_a_job_removed_before_its_cancel(monkeypatch, rq_server):
    """A job whose record goes between its fetch and the cancel's read is reported not found."""
    job = _enqueue(rq_server)
    fetch = Job.fetch

    def _fetch_then_removed(job_id: str, connection: Any = None, serializer: Any = None) -> Job:
        fetched = fetch(job_id, connection=connection, serializer=serializer)
        connection.delete(fetched.key)
        return fetched

    monkeypatch.setattr(Job, "fetch", staticmethod(_fetch_then_removed))

    assert await tools.backend_cancel_task(job.id) == f"Task {job.id} not found"


async def test_cancel_task_with_terminate_reports_a_claimed_job_removed_before_it_starts(rq_server, stops):
    job = _enqueue(rq_server)
    _claim(rq_server, job)

    async def _removed_later() -> None:
        await asyncio.sleep(0.3)
        rq_server.delete(job.key)

    removing = asyncio.create_task(_removed_later())
    assert await tools.backend_cancel_task(job.id, terminate=True) == f"Task {job.id} not found"
    await removing
    assert stops == []


async def test_cancel_task_with_terminate_reports_a_job_removed_before_the_stop(monkeypatch, rq_server):
    """RQ's stop-job command finds no job once its record is gone; the reply says so."""
    job = _enqueue(rq_server)
    _start(rq_server, job)
    send = inspection.send_stop_job_command

    def _removed_first(conn: Any, job_id: str, serializer: Any = None) -> None:
        conn.delete(job.key)
        send(conn, job_id)

    monkeypatch.setattr(inspection, "send_stop_job_command", _removed_first)

    assert await tools.backend_cancel_task(job.id, terminate=True) == f"Task {job.id} not found"


async def test_cancel_task_with_terminate_reports_a_stop_not_confirmed(monkeypatch, rq_server):
    """A stop the worker does not carry out within the task timeout is reported with the status the
    job still reads, never as stopped."""
    monkeypatch.setenv("RQ_TASK_TIMEOUT", "1")
    rq_settings.cache_clear()
    sent: list[str] = []
    monkeypatch.setattr(inspection, "send_stop_job_command", lambda conn, job_id, serializer=None: sent.append(job_id))
    job = _enqueue(rq_server)
    _start(rq_server, job)

    try:
        out = await tools.backend_cancel_task(job.id, terminate=True)
    finally:
        monkeypatch.delenv("RQ_TASK_TIMEOUT")
        rq_settings.cache_clear()
    assert out == f"Task {job.id} stop requested but not confirmed (status: started)"
    assert sent == [job.id]


async def test_cancel_task_with_terminate_reports_a_claimed_job_not_started_in_time(monkeypatch, rq_server, stops):
    """A claimed job that does not start within the task timeout reads as a stop not confirmed, the
    same as a running job the stop does not end in time."""
    monkeypatch.setenv("RQ_TASK_TIMEOUT", "1")
    rq_settings.cache_clear()
    job = _enqueue(rq_server)
    _claim(rq_server, job)

    try:
        out = await tools.backend_cancel_task(job.id, terminate=True)
    finally:
        monkeypatch.delenv("RQ_TASK_TIMEOUT")
        rq_settings.cache_clear()
    assert out == f"Task {job.id} stop requested but not confirmed (status: queued)"
    assert stops == []


def _recurring(conn: Any) -> Job:
    return scheduler_for(conn).schedule(datetime.now(UTC) + timedelta(minutes=5), "os.getpid", interval=60)


@pytest.mark.parametrize("terminate", [False, True])
async def test_cancel_task_cancels_a_recurring_job(rq_server, terminate):
    job = _recurring(rq_server)

    out = await tools.backend_cancel_task(job.id, terminate=terminate)
    assert out == f"Scheduled Task {job.id} canceled"
    assert job.id not in scheduler_for(rq_server)


async def test_cancel_task_cancels_a_recurring_job_and_its_queued_run(rq_server):
    job = _recurring(rq_server)
    _queue(rq_server).enqueue_job(job)

    assert await tools.backend_cancel_task(job.id) == f"Scheduled Task {job.id} canceled"
    assert job.id not in scheduler_for(rq_server)
    assert _canceled(rq_server, job)


async def test_cancel_task_without_terminate_leaves_a_recurring_jobs_running_run_to_finish(rq_server):
    job = _recurring(rq_server)
    _queue(rq_server).enqueue_job(job)
    _start(rq_server, job)

    out = await tools.backend_cancel_task(job.id)
    assert out == f"Scheduled Task {job.id} canceled; {_left_to_finish(job.id)}"
    assert job.id not in scheduler_for(rq_server)
    assert _status(rq_server, job.id) == JobStatus.STARTED


async def test_cancel_task_with_terminate_stops_a_recurring_jobs_running_run(rq_server, stops):
    job = _recurring(rq_server)
    _queue(rq_server).enqueue_job(job)
    _start(rq_server, job)

    out = await tools.backend_cancel_task(job.id, terminate=True)
    assert out == f"Scheduled Task {job.id} canceled; Task {job.id} stopped"
    assert job.id not in scheduler_for(rq_server)
    assert stops == [job.id]
