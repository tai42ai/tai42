"""The RQ task door: the task options mapped onto RQ's enqueue API, and the wait on a job."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import fakeredis
import pytest
from rq.exceptions import NoSuchJobError
from rq.job import Job, JobStatus
from tai42_contract.backend import TaskWaitTimeoutError
from tai42_kit.backend import CallbackSchema
from tai42_kit.settings import reset_all_settings
from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

from tai42_backend_rq import task_door
from tai42_backend_rq.queue import TaskBudgetQueue
from tai42_backend_rq.task_door import RqTaskDoor, _poll_job_result
from tai42_backend_rq.tasks import callback_job, tool_execution

from .conftest import make_client_ctx


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
        # The last status holds once the scripted ones run out, as a stored job status does.
        self._status = next(self._statuses, self._status)

    def get_status(self) -> str:
        return self._status

    def return_value(self) -> Any:
        return self._result

    def latest_result(self) -> str:
        return "stack trace"


def test_wait_returns_finished_result() -> None:
    job = FakeJob([JobStatus.FINISHED], result={"done": True})
    assert _poll_job_result(job, timeout=5) == {"done": True}  # type: ignore[arg-type]


def test_wait_polls_until_finished() -> None:
    job = FakeJob([JobStatus.STARTED, JobStatus.FINISHED], result="late")
    assert _poll_job_result(job, timeout=5) == "late"  # type: ignore[arg-type]


def test_wait_raises_on_failed_job() -> None:
    job = FakeJob([JobStatus.FAILED])
    with pytest.raises(RuntimeError, match="Job job-1 failed: stack trace"):
        _poll_job_result(job, timeout=5)  # type: ignore[arg-type]


def test_wait_failed_job_raises_with_the_stored_exc_string() -> None:
    """A failed job's persisted traceback text is what the raised error carries."""

    class _Result:
        exc_string = "Traceback ...\nValueError: boom"

    job = FakeJob([JobStatus.FAILED])
    job.latest_result = lambda: _Result()  # type: ignore[method-assign,assignment,return-value]
    with pytest.raises(RuntimeError, match="ValueError: boom"):
        _poll_job_result(job, timeout=5)  # type: ignore[arg-type]


def test_wait_raises_on_vanished_job() -> None:
    job = FakeJob([JobStatus.STARTED])
    job.refresh_error = NoSuchJobError("gone")
    with pytest.raises(RuntimeError, match="Job job-1 not found"):
        _poll_job_result(job, timeout=5)  # type: ignore[arg-type]


@pytest.mark.parametrize("status", [JobStatus.STOPPED, JobStatus.CANCELED])
def test_wait_on_a_job_ended_without_a_result_raises_at_once_naming_its_status(status: JobStatus) -> None:
    """A stopped or cancelled job has ended: the wait raises its status, never the wait timeout."""
    job = FakeJob([JobStatus.STARTED, status])
    with pytest.raises(RuntimeError, match=rf"^Job job-1 did not complete \(status: {status.value}\)$"):
        _poll_job_result(job, timeout=2)  # type: ignore[arg-type]


def test_wait_running_out_on_an_unfinished_job_raises_the_wait_timeout() -> None:
    job = FakeJob([JobStatus.STARTED, JobStatus.STARTED])
    with pytest.raises(TaskWaitTimeoutError, match="job job-1 still started after 0 seconds"):
        _poll_job_result(job, timeout=0)  # type: ignore[arg-type]


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> Iterator[fakeredis.FakeStrictRedis]:
    """An in-memory Redis the door enqueues into, under the default settings queue."""
    monkeypatch.delenv("RQ_QUEUE_NAME", raising=False)
    reset_all_settings()
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    monkeypatch.setattr(task_door, "client_ctx", make_client_ctx(conn))
    try:
        yield conn
    finally:
        reset_all_settings()


async def test_submit_enqueues_the_tool_execution_job_with_its_arguments(redis: Any) -> None:
    job = await RqTaskDoor().submit((1,), {"backend_tool_name": "t", "x": 2}, {}, None)

    stored = Job.fetch(job.id, connection=redis)
    assert stored.func is tool_execution
    assert stored.args == (1,)
    assert stored.kwargs == {"backend_tool_name": "t", "x": 2}
    assert RqTaskDoor().task_id(job) == job.id


async def test_a_tool_argument_named_like_an_rq_job_option_stays_a_tool_argument(redis: Any) -> None:
    job = await RqTaskDoor().submit((), {"backend_tool_name": "t", "ttl": 7, "description": "d"}, {}, None)

    assert Job.fetch(job.id, connection=redis).kwargs == {"backend_tool_name": "t", "ttl": 7, "description": "d"}


async def test_expires_becomes_the_jobs_ttl(redis: Any) -> None:
    job = await RqTaskDoor().submit((), {"backend_tool_name": "t"}, {"expires": "90.5"}, None)

    assert Job.fetch(job.id, connection=redis).ttl == 90


async def test_a_callback_is_a_callback_job_depending_on_the_job(redis: Any) -> None:
    callback = CallbackSchema(tool="next", carried_kwargs={WORKER_SECRET_CAPABILITY_ARG: False})

    job = await RqTaskDoor().submit((), {"backend_tool_name": "t"}, {}, callback)

    [callback_id] = TaskBudgetQueue("default", connection=redis).deferred_job_registry.get_job_ids()
    follow_up = Job.fetch(callback_id, connection=redis)
    assert follow_up.func is callback_job
    assert follow_up.args[0] == job.id
    assert follow_up.dependency_ids == [job.id]
