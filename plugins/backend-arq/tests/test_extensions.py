"""The sync/async/schedule task extension factories and their branch tools."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import orjson
import pytest

from tai42_backend_arq import extensions, scheduler
from tai42_backend_arq.settings import TaskFailedError
from tai42_backend_arq.tasks import ARQ_SCHEDULE_OPTS, ARQ_TASK_OPTS


async def sample_tool(text: str, count: int = 1) -> str:
    """Repeat ``text`` ``count`` times."""
    return text * count


class _FakeArq:
    """Records enqueue calls and answers with a configurable job."""

    def __init__(self, result: Any = "done") -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self._result = result

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        result = self._result

        class _Job:
            job_id = "job-1"

            async def result(self, timeout: float | None = None) -> Any:
                if isinstance(result, BaseException):
                    raise result
                return result

        return _Job()


def test_sync_task_branch_signature() -> None:
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")
    assert branch.__name__ == "sample_tool_sync_task"
    params = inspect.signature(branch).parameters
    assert "text" in params
    assert "count" in params
    for opt in ARQ_TASK_OPTS:
        assert opt in params


async def test_sync_task_runs_and_returns_result(bind_pool) -> None:
    fake = _FakeArq(result={"ok": True})
    bind_pool(fake)
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")

    out = await branch(text="hi", count=2)

    assert out == {"ok": True}
    ((args, kwargs),) = fake.calls
    assert args == ("tool_execution",)
    assert kwargs == {"text": "hi", "count": 2, "backend_tool_name": "sample_tool"}


async def test_sync_task_timeout_raises_clear_error(bind_pool) -> None:
    fake = _FakeArq(result=TimeoutError())
    bind_pool(fake)
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")

    with pytest.raises(TimeoutError, match="did not complete within 300 seconds"):
        await branch(text="hi")


async def test_sync_task_failed_job_reraises_revived_failure(bind_pool) -> None:
    """A failed job replays its revived stored failure whole out of the wait."""
    fake = _FakeArq(result=TaskFailedError("ValueError", "ValueError('boom')", None))
    bind_pool(fake)
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")

    with pytest.raises(TaskFailedError, match=r"ValueError\('boom'\)"):
        await branch(text="hi")


async def test_sync_task_aborted_job_raises_detail_not_cancellation(bind_pool) -> None:
    """An aborted job's stored abort replays as a revived ``CancelledError``;
    the wait re-raises it as ``TaskFailedError`` — a raw CancelledError
    escaping the branch tool would read as a cancellation of the call itself."""
    fake = _FakeArq(result=asyncio.CancelledError("CancelledError()"))
    bind_pool(fake)
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")

    with pytest.raises(TaskFailedError, match=r"CancelledError\(\)"):
        await branch(text="hi")


async def test_async_task_returns_submission(bind_pool) -> None:
    fake = _FakeArq()
    bind_pool(fake)
    branch = extensions.async_task(sample_tool, "sample_tool", "doc")

    out = await branch(text="hi")

    assert out == {"task_id": "job-1", "status": "submitted"}
    assert branch.__name__ == "sample_tool_async_task"
    assert "Async version of 'sample_tool'" in branch.__doc__


async def test_async_task_enqueue_opts_split(bind_pool) -> None:
    fake = _FakeArq()
    bind_pool(fake)
    branch = extensions.async_task(sample_tool, "sample_tool", "doc")

    await branch(text="hi", countdown=30, expires=None, callback_kwargs={"tool": "next"})

    ((_args, kwargs),) = fake.calls
    # countdown maps to arq's _defer_by enqueue option, callback_kwargs stays a
    # job kwarg, and a None option is dropped entirely.
    assert kwargs["_defer_by"] == 30
    assert kwargs["callback_kwargs"] == {"tool": "next"}
    assert "countdown" not in kwargs
    assert "expires" not in kwargs
    assert "_expires" not in kwargs


async def test_async_task_eta_and_expires_map_to_arq_options(bind_pool) -> None:
    from datetime import UTC, datetime

    fake = _FakeArq()
    bind_pool(fake)
    branch = extensions.async_task(sample_tool, "sample_tool", "doc")

    await branch(text="hi", eta="2030-01-02T03:04:05+00:00", expires=90.0)

    ((_args, kwargs),) = fake.calls
    # eta (ISO datetime) maps to _defer_until; expires (seconds) to _expires.
    assert kwargs["_defer_until"] == datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert kwargs["_expires"] == 90
    assert "eta" not in kwargs
    assert "expires" not in kwargs


async def test_async_task_eta_wins_over_countdown(bind_pool) -> None:
    fake = _FakeArq()
    bind_pool(fake)
    branch = extensions.async_task(sample_tool, "sample_tool", "doc")

    await branch(text="hi", eta="2030-01-02T03:04:05+00:00", countdown=30)

    ((_args, kwargs),) = fake.calls
    assert "_defer_until" in kwargs
    assert "_defer_by" not in kwargs


async def test_schedule_task_writes_interval_schedule(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)

    class _NoJob:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def abort(self, **kwargs: Any) -> bool:
            return True

    monkeypatch.setattr(scheduler, "Job", _NoJob)
    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    assert branch.__name__ == "sample_tool_schedule_task"
    params = inspect.signature(branch).parameters
    for opt in ARQ_SCHEDULE_OPTS:
        assert opt in params

    await branch(text="hi", backend_schedule_name="every-min", backend_schedule=60)

    stored = fake_redis._store["arq:schedule:every-min"]
    assert stored[b"target"] == b"tool_execution"
    assert stored[b"cron_or_interval"] == b"60"
    assert stored[b"enabled"] == b"true"
    assert orjson.loads(stored[b"schedule"]) == {"__type__": "interval", "every": 60.0, "relative": False}
    # ``count`` appears with its default: the branch presents the tool's real
    # signature, so bound defaults materialize in the stored kwargs.
    assert orjson.loads(stored[b"kwargs"]) == {"text": "hi", "count": 1, "backend_tool_name": "sample_tool"}


async def test_schedule_task_requires_name_and_schedule(fake_redis, bind_pool) -> None:
    bind_pool(fake_redis)
    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    with pytest.raises(ValueError, match="backend_schedule_name is required"):
        await branch(text="hi", backend_schedule=60)
    with pytest.raises(ValueError, match="backend_schedule is required"):
        await branch(text="hi", backend_schedule_name="every-min")
    assert fake_redis._store == {}


async def test_schedule_task_writes_crontab_schedule(fake_redis, monkeypatch, bind_pool) -> None:
    bind_pool(fake_redis)

    class _NoJob:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def abort(self, **kwargs: Any) -> bool:
            return True

    monkeypatch.setattr(scheduler, "Job", _NoJob)
    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")

    await branch(text="hi", backend_schedule_name="mornings", backend_schedule="0 9 * * 1")

    stored = fake_redis._store["arq:schedule:mornings"]
    assert stored[b"cron_or_interval"] == b"0 9 * * 1"
    schedule = orjson.loads(stored[b"schedule"])
    assert schedule["__type__"] == "crontab"
    assert schedule["hour"] == "9"
