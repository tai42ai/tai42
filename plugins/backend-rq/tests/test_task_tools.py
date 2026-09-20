"""RQ task and worker query tools: ping, task status/result across states, and the
queue/worker statistics.
"""

from __future__ import annotations

import base64
import pickle
import zlib
from datetime import UTC, datetime
from typing import Any

import pytest
from rq.job import JobStatus

from tai42_backend_rq import tools
from tai42_backend_rq.settings import rq_settings

from .conftest import (
    FakeAsyncRedis,
    _patch_redis,
)


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


async def test_active_reserved_scheduled_stats_queues(monkeypatch):
    settings = rq_settings()
    redis = FakeAsyncRedis(
        hashes={
            settings.rq_worker_key("w1"): {
                "current_job": b"j1",
                "queues": b'["default","high"]',
                "state": b"busy",
            },
            settings.rq_worker_key("w2"): {"queues": b"default,low"},
            settings.rq_job_key("j1"): {b"status": b"started"},
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

    active = await tools.backend_active_tasks("w1")
    assert active == {"w1": [{"id": "j1", "data": {"status": "started"}}]}
    # All-workers path: w2 has no current job.
    assert set(await tools.backend_active_tasks()) == {"w1"}

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
