"""The three backend extension factories and the branch tools they mint."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from rq.exceptions import NoSuchJobError
from rq.job import JobStatus

from tai42_backend_rq import extensions
from tai42_backend_rq.settings import rq_settings

from .conftest import make_client_ctx


async def sample_tool(x: int, note: str = "hi") -> dict[str, Any]:
    """Sample tool used as the extension target."""
    return {"x": x, "note": note}


class FakeJob:
    def __init__(self, statuses: list[str], result: Any = None, job_id: str = "job-1") -> None:
        self._statuses = iter(statuses)
        self._status: Any = None
        self._result = result
        self.id = job_id
        self.refresh_error: Exception | None = None

    def refresh(self) -> None:
        if self.refresh_error is not None:
            raise self.refresh_error
        self._status = next(self._statuses)

    def get_status(self) -> str:
        return self._status

    def return_value(self) -> Any:
        return self._result

    def latest_result(self) -> str:
        return "stack trace"


# --- _wait_for_job_result ----------------------------------------------------


def test_wait_returns_finished_result():
    job = FakeJob([JobStatus.FINISHED], result={"done": True})
    assert extensions._wait_for_job_result(job, timeout=5) == {"done": True}


def test_wait_polls_until_finished():
    job = FakeJob([JobStatus.STARTED, JobStatus.FINISHED], result="late")
    assert extensions._wait_for_job_result(job, timeout=5) == "late"


def test_wait_raises_on_failed_job():
    job = FakeJob([JobStatus.FAILED])
    with pytest.raises(RuntimeError, match="Job job-1 failed: stack trace"):
        extensions._wait_for_job_result(job, timeout=5)


def test_wait_failed_job_raises_with_the_stored_exc_string():
    """A failed job's persisted traceback text is what the raised error carries."""

    class _Result:
        exc_string = "Traceback ...\nValueError: boom"

    job = FakeJob([JobStatus.FAILED])
    job.latest_result = lambda: _Result()  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="ValueError: boom"):
        extensions._wait_for_job_result(job, timeout=5)


def test_wait_raises_on_vanished_job():
    job = FakeJob([JobStatus.STARTED])
    job.refresh_error = NoSuchJobError("gone")
    with pytest.raises(RuntimeError, match="Job job-1 not found"):
        extensions._wait_for_job_result(job, timeout=5)


def test_wait_times_out():
    job = FakeJob([JobStatus.STARTED, JobStatus.STARTED])
    with pytest.raises(TimeoutError, match="did not complete within 0 seconds"):
        extensions._wait_for_job_result(job, timeout=0)


# --- factories ---------------------------------------------------------------


def test_sync_task_branch_shape():
    branch = extensions.sync_task(sample_tool, "sample_tool", "the doc")
    assert branch.__name__ == "sample_tool_sync_task"
    params = inspect.signature(branch).parameters
    assert set(params) == {"x", "note", "countdown", "expires", "eta", "callback_kwargs"}
    assert params["countdown"].kind is inspect.Parameter.KEYWORD_ONLY
    assert branch.__doc__ == "the doc"


async def test_sync_task_branch_queues_and_waits(monkeypatch):
    enqueued: list[dict[str, Any]] = []
    job = FakeJob([JobStatus.FINISHED], result={"ran": True})

    async def fake_enqueue(*args: Any, **kwargs: Any) -> FakeJob:
        enqueued.append(kwargs)
        return job

    monkeypatch.setattr(extensions, "enqueue_task", fake_enqueue)

    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")
    result = await branch(x=3, countdown=9)

    assert result == {"ran": True}
    [kwargs] = enqueued
    # The dispatch tool name is injected under the configured kwarg.
    assert kwargs[rq_settings().tool_name_arg] == "sample_tool"
    assert kwargs["x"] == 3
    assert kwargs["countdown"] == 9


async def test_async_task_branch_returns_task_id(monkeypatch):
    async def fake_enqueue(*args: Any, **kwargs: Any) -> FakeJob:
        return FakeJob([], job_id="job-42")

    monkeypatch.setattr(extensions, "enqueue_task", fake_enqueue)

    branch = extensions.async_task(sample_tool, "sample_tool", "doc")
    assert branch.__name__ == "sample_tool_async_task"
    assert "Async version of 'sample_tool'" in branch.__doc__
    assert await branch(x=1) == {"task_id": "job-42", "status": "submitted"}


async def test_schedule_task_branch_applies_schedule(monkeypatch):
    applied: list[tuple[Any, ...]] = []

    async def fake_apply(scheduler: Any, norm: Any, func: Any, args: Any, kwargs: Any, name: Any) -> None:
        applied.append((norm, func, args, kwargs, name))

    monkeypatch.setattr(extensions, "apply_normalized_schedule", fake_apply)
    monkeypatch.setattr(extensions, "client_ctx", make_client_ctx(object()))
    monkeypatch.setattr(extensions, "Scheduler", lambda queue_name=None, connection=None: object())

    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    assert branch.__name__ == "sample_tool_schedule_task"
    params = inspect.signature(branch).parameters
    assert {"backend_schedule_name", "backend_schedule"} <= set(params)

    await branch(x=7, backend_schedule_name="nightly", backend_schedule=3600)

    [(norm, func, _args, kwargs, name)] = applied
    assert norm == {"__type__": "interval", "every": 3600.0, "relative": False}
    assert func is extensions.tool_execution
    assert name == "nightly"
    # The queued job dispatches back onto the original tool by name.
    assert kwargs[rq_settings().tool_name_arg] == "sample_tool"
    assert kwargs["x"] == 7


async def test_schedule_task_binds_the_settings_queue_name(monkeypatch):
    """The scheduler enqueues due jobs onto the ``queue_name`` setting's queue, so
    a schedule fires onto the SAME per-stack queue the worker consumes — without
    it, a namespaced worker would never see its own scheduled jobs."""
    from tai42_backend_rq.settings import RqSettings

    captured: dict[str, Any] = {}

    def fake_scheduler(queue_name: Any = None, connection: Any = None) -> Any:
        captured["queue_name"] = queue_name
        return object()

    async def fake_apply(scheduler: Any, norm: Any, func: Any, args: Any, kwargs: Any, name: Any) -> None:
        pass

    monkeypatch.setattr(extensions, "rq_settings", lambda: RqSettings(queue_name="tai42_e2e_abc123:default"))
    monkeypatch.setattr(extensions, "apply_normalized_schedule", fake_apply)
    monkeypatch.setattr(extensions, "client_ctx", make_client_ctx(object()))
    monkeypatch.setattr(extensions, "Scheduler", fake_scheduler)

    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    await branch(x=7, backend_schedule_name="nightly", backend_schedule=3600)

    assert captured["queue_name"] == "tai42_e2e_abc123:default"


async def test_schedule_task_requires_name_and_schedule():
    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    with pytest.raises(ValueError, match="backend_schedule_name is required"):
        await branch(x=1, backend_schedule=30)
    with pytest.raises(ValueError, match="backend_schedule is required"):
        await branch(x=1, backend_schedule_name="nightly")


async def test_schedule_task_rejects_bad_schedule(monkeypatch):
    monkeypatch.setattr(extensions, "client_ctx", make_client_ctx(object()))
    monkeypatch.setattr(extensions, "Scheduler", lambda queue_name=None, connection=None: object())

    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    with pytest.raises(ValueError, match="Unsupported schedule format"):
        await branch(x=1, backend_schedule_name="s", backend_schedule={"type": "bogus"})
