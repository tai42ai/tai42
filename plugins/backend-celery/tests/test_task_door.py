"""The Celery task door: the task options as ``apply_async`` keywords, and the wait on a result."""

from __future__ import annotations

from typing import Any

import pytest
from celery.exceptions import TimeoutError as CeleryTimeoutError
from tai42_contract.backend import CallbackSchema, TaskWaitTimeoutError
from tai42_kit.settings import reset_all_settings

from tai42_backend_celery.core import task_door
from tai42_backend_celery.core.task_door import CeleryTaskDoor


class _FakeAsyncResult:
    def __init__(self, value: Any = None, exc: Exception | None = None) -> None:
        self.id = "task-123"
        self.status = "STARTED"
        self._value = value
        self._exc = exc
        self.get_calls: list[Any] = []

    def get(self, timeout: float | None = None) -> Any:
        self.get_calls.append(timeout)
        if self._exc is not None:
            raise self._exc
        return self._value


class _FakeTask:
    def __init__(self) -> None:
        self.result = _FakeAsyncResult(value="worker-result")
        self.calls: list[dict[str, Any]] = []

    def apply_async(self, args: Any = None, kwargs: Any = None, **opts: Any) -> _FakeAsyncResult:
        self.calls.append({"args": args, "kwargs": kwargs, "opts": opts})
        return self.result


@pytest.fixture
def fake_task(monkeypatch: pytest.MonkeyPatch) -> _FakeTask:
    task = _FakeTask()
    monkeypatch.setattr(task_door, "tool_execution", task)
    return task


async def test_submit_sends_the_tool_kwargs_to_the_settings_queue(fake_task: _FakeTask) -> None:
    handle = await CeleryTaskDoor().submit(("a",), {"x": 1}, {}, None)

    assert handle is fake_task.result
    assert fake_task.calls == [{"args": ("a",), "kwargs": {"x": 1}, "opts": {"queue": "celery"}}]


async def test_a_queue_label_sends_to_the_derived_queue(fake_task: _FakeTask) -> None:
    await CeleryTaskDoor().submit((), {}, {"queue": "q1", "priority": 3, "routing_key": "rk"}, None)

    [call] = fake_task.calls
    assert call["opts"] == {"queue": "celery.q1", "priority": 3, "routing_key": "rk"}


async def test_the_queue_derives_from_the_queue_name_setting(
    fake_task: _FakeTask, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CELERY_QUEUE_NAME", "stack-1")
    reset_all_settings()
    try:
        await CeleryTaskDoor().submit((), {}, {"queue": "gpu"}, None)
    finally:
        reset_all_settings()

    [call] = fake_task.calls
    assert call["opts"]["queue"] == "stack-1.gpu"


async def test_a_label_outside_the_grammar_sends_nothing(fake_task: _FakeTask) -> None:
    with pytest.raises(ValueError, match=r"must match \[A-Za-z0-9_-\]\+"):
        await CeleryTaskDoor().submit((), {}, {"queue": "a.b"}, None)

    assert fake_task.calls == []


async def test_a_callback_becomes_a_link_running_the_callback_task(fake_task: _FakeTask) -> None:
    callback = CallbackSchema(tool="follow_up")

    await CeleryTaskDoor().submit((), {}, {}, callback)

    [call] = fake_task.calls
    assert call["opts"]["link"].task == task_door.callback_task.name
    assert call["opts"]["link"].args == (callback,)


async def test_a_callback_runs_on_the_task_s_own_derived_queue(fake_task: _FakeTask) -> None:
    await CeleryTaskDoor().submit((), {}, {"queue": "gpu"}, CallbackSchema(tool="follow_up"))

    [call] = fake_task.calls
    assert call["opts"]["queue"] == "celery.gpu"
    assert call["opts"]["link"].options["queue"] == "celery.gpu"


async def test_a_callback_without_a_label_runs_on_the_settings_queue(fake_task: _FakeTask) -> None:
    await CeleryTaskDoor().submit((), {}, {}, CallbackSchema(tool="follow_up"))

    [call] = fake_task.calls
    assert call["opts"]["link"].options["queue"] == "celery"


async def test_wait_returns_the_result_within_the_timeout() -> None:
    result: Any = _FakeAsyncResult(value="worker-result")

    assert await CeleryTaskDoor().wait(result, 7) == "worker-result"
    assert result.get_calls == [7]


async def test_wait_running_out_raises_the_wait_timeout() -> None:
    result: Any = _FakeAsyncResult(exc=CeleryTimeoutError("timed out"))

    with pytest.raises(TaskWaitTimeoutError, match=r"^task task-123 still STARTED after 5 seconds$") as raised:
        await CeleryTaskDoor().wait(result, 5)
    assert isinstance(raised.value.__cause__, CeleryTimeoutError)


async def test_wait_raises_the_tasks_own_exception() -> None:
    result: Any = _FakeAsyncResult(exc=ValueError("boom"))
    with pytest.raises(ValueError, match="boom"):
        await CeleryTaskDoor().wait(result, 5)
