"""The arq task door: the task options mapped onto arq's enqueue API, and the wait on a job."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from tai42_contract.backend import CallbackSchema, TaskWaitTimeoutError

from tai42_backend_arq import task_door
from tai42_backend_arq.settings import TaskFailedError
from tai42_backend_arq.task_door import ArqJobRef, ArqTaskDoor

from .conftest import SETTINGS_QUEUE


class _RecordingArq:
    """Records each enqueue and answers with a job, or with ``None`` as arq does for a taken id."""

    def __init__(self, *, refuse: bool = False) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._refuse = refuse

    async def sadd(self, *args: Any) -> None:
        pass

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        if self._refuse:
            return None

        class _Job:
            job_id = "job-1"

        return _Job()


@pytest.fixture
def arq(bind_pool: Any) -> _RecordingArq:
    fake = _RecordingArq()
    bind_pool(fake)
    return fake


async def test_submit_enqueues_tool_execution_with_the_tool_kwargs(arq: _RecordingArq) -> None:
    ref = await ArqTaskDoor().submit(("a",), {"text": "hi"}, {}, None)

    assert ref == ArqJobRef(job_id="job-1", queue_name="arq:queue")
    assert arq.calls == [(("tool_execution", "a"), {"_queue_name": "arq:queue", "text": "hi"})]


async def test_countdown_becomes_defer_by(arq: _RecordingArq) -> None:
    await ArqTaskDoor().submit((), {}, {"countdown": 30}, None)

    [(_args, kwargs)] = arq.calls
    assert kwargs["_defer_by"] == 30


async def test_eta_becomes_defer_until_and_wins_over_countdown(arq: _RecordingArq) -> None:
    await ArqTaskDoor().submit((), {}, {"eta": "2030-01-02T03:04:05+00:00", "countdown": 30}, None)

    [(_args, kwargs)] = arq.calls
    assert kwargs["_defer_until"] == datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert "_defer_by" not in kwargs


async def test_expires_becomes_whole_second_expires(arq: _RecordingArq) -> None:
    await ArqTaskDoor().submit((), {}, {"expires": 90.5}, None)

    [(_args, kwargs)] = arq.calls
    assert kwargs["_expires"] == 90


async def test_a_callback_rides_as_the_jobs_callback_kwargs(arq: _RecordingArq) -> None:
    callback = CallbackSchema(tool="next")
    await ArqTaskDoor().submit((), {"text": "hi"}, {}, callback)

    [(_args, kwargs)] = arq.calls
    assert kwargs["callback_kwargs"] is callback
    assert kwargs["text"] == "hi"


async def test_an_enqueue_arq_refuses_raises(bind_pool: Any) -> None:
    bind_pool(_RecordingArq(refuse=True))

    with pytest.raises(RuntimeError, match="arq refused to enqueue the job on queue 'arq:queue'"):
        await ArqTaskDoor().submit((), {}, {}, None)


async def test_a_labelled_submit_waits_out_as_the_wait_timeout(arq_redis: Any) -> None:
    door = ArqTaskDoor()
    ref = await door.submit((), {"backend_tool_name": "t"}, {"queue": "gpu"}, None)

    assert ref.queue_name == f"{SETTINGS_QUEUE}.gpu"
    with pytest.raises(TaskWaitTimeoutError, match=f"^job {ref.job_id} still unfinished after 0.2 seconds$"):
        await door.wait(ref, 0.2)


class _StoredJob:
    """A job whose result replays ``outcome``: a value, or a stored failure raised."""

    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome

    async def result(self, timeout: float | None = None) -> Any:
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome


def _bind_stored(monkeypatch: pytest.MonkeyPatch, bind_pool: Any, outcome: Any) -> None:
    class _Redis:
        job_deserializer = None

    bind_pool(_Redis())
    monkeypatch.setattr(task_door, "Job", lambda job_id, redis, **_: _StoredJob(outcome))


async def test_wait_returns_the_jobs_result(monkeypatch: pytest.MonkeyPatch, bind_pool: Any) -> None:
    _bind_stored(monkeypatch, bind_pool, {"ok": True})

    assert await ArqTaskDoor().wait(ArqJobRef("job-1", "arq:queue"), 5) == {"ok": True}


async def test_wait_raises_a_failed_jobs_revived_failure(monkeypatch: pytest.MonkeyPatch, bind_pool: Any) -> None:
    _bind_stored(monkeypatch, bind_pool, TaskFailedError("ValueError", "ValueError('boom')", None))

    with pytest.raises(TaskFailedError, match=r"ValueError\('boom'\)"):
        await ArqTaskDoor().wait(ArqJobRef("job-1", "arq:queue"), 5)


async def test_wait_raises_an_aborted_job_as_a_failure_not_a_cancellation(
    monkeypatch: pytest.MonkeyPatch, bind_pool: Any
) -> None:
    """An aborted job replays its stored abort as a revived ``CancelledError``; a raw one escaping
    the wait would read as a cancellation of the caller."""
    _bind_stored(monkeypatch, bind_pool, asyncio.CancelledError("CancelledError()"))

    with pytest.raises(TaskFailedError, match=r"CancelledError\(\)"):
        await ArqTaskDoor().wait(ArqJobRef("job-1", "arq:queue"), 5)
