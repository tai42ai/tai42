"""The three BACKEND tool extensions: registration, signatures, and dispatch."""

from __future__ import annotations

import inspect
from typing import Any, ClassVar

import pytest
from celery.exceptions import TimeoutError as CeleryTimeoutError
from celery.schedules import crontab, schedule
from tai42_contract.extensions import ExtensionKind

import tai42_backend_celery.extensions.extensions as extensions


def sample_tool(a: int, b: str = "x") -> str:
    """A sample tool."""
    return f"{a}-{b}"


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
    def __init__(self, result: _FakeAsyncResult) -> None:
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def apply_async(self, args: Any = None, kwargs: Any = None, **opts: Any) -> _FakeAsyncResult:
        self.calls.append({"args": args, "kwargs": kwargs, "opts": opts})
        return self._result


@pytest.fixture
def fake_task(monkeypatch: pytest.MonkeyPatch) -> _FakeTask:
    task = _FakeTask(_FakeAsyncResult(value="worker-result"))
    monkeypatch.setattr(extensions, "tool_execution", task)
    return task


def test_extensions_registered_as_backend_kind(stub_app) -> None:
    registered = stub_app.extensions.registered
    for name in ("sync_task", "schedule_task", "async_task"):
        kind, factory = registered[name]
        assert kind is ExtensionKind.BACKEND
        assert callable(factory)


@pytest.mark.parametrize(
    ("factory", "suffix"),
    [(extensions.sync_task, "sync_task"), (extensions.async_task, "async_task")],
)
def test_dispatch_branches_expose_task_opts(factory, suffix) -> None:
    branch = factory(sample_tool, "sample_tool", "doc")
    assert branch.__name__ == f"sample_tool_{suffix}"
    params = inspect.signature(branch).parameters
    assert {"a", "b"} <= set(params)
    for opt in ("queue", "countdown", "priority", "retry", "routing_key", "expires", "eta", "callback_kwargs"):
        assert opt in params
        assert params[opt].default is None


async def test_sync_task_dispatches_and_returns_result(fake_task) -> None:
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")
    out = await branch(a=1, queue="q1")
    assert out == "worker-result"
    (call,) = fake_task.calls
    # makefun materializes the wrapped tool's defaulted params in the call.
    assert call["kwargs"] == {"a": 1, "b": "x", "backend_tool_name": "sample_tool", "backend_secret_capability": False}
    assert call["opts"] == {"queue": "q1"}
    # The wait is bounded by the configured task timeout.
    assert fake_task._result.get_calls == [extensions.celery_settings().task_timeout]


async def test_sync_task_timeout_raises_builtin_timeout_with_task_id(fake_task) -> None:
    fake_task._result._exc = CeleryTimeoutError("timed out")
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")
    with pytest.raises(TimeoutError, match="task-123") as excinfo:
        await branch(a=1)
    assert isinstance(excinfo.value.__cause__, CeleryTimeoutError)


async def test_sync_task_failure_reraises_the_tasks_own_exception(fake_task) -> None:
    fake_task._result._exc = ValueError("boom")
    branch = extensions.sync_task(sample_tool, "sample_tool", "doc")
    with pytest.raises(ValueError, match="boom"):
        await branch(a=1)


async def test_async_task_returns_submission(fake_task) -> None:
    branch = extensions.async_task(sample_tool, "sample_tool", "doc")
    out = await branch(a=2)
    assert out == {"task_id": "task-123", "status": "submitted"}
    (call,) = fake_task.calls
    assert call["kwargs"] == {"a": 2, "b": "x", "backend_tool_name": "sample_tool", "backend_secret_capability": False}
    assert call["opts"] == {}


async def test_callback_kwargs_becomes_link_signature(fake_task, monkeypatch) -> None:
    linked: list[Any] = []
    fake_signature = type("_S", (), {"s": staticmethod(lambda cb: linked.append(cb) or "sig")})()
    monkeypatch.setattr(extensions, "callback_task", fake_signature)
    branch = extensions.async_task(sample_tool, "sample_tool", "doc")
    callback = extensions.CallbackSchema(tool="follow_up")
    await branch(a=1, callback_kwargs=callback)
    (call,) = fake_task.calls
    assert call["opts"] == {"link": "sig"}
    assert linked == [callback]


class _FakeEntry:
    instances: ClassVar[list[_FakeEntry]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.saved = False
        type(self).instances.append(self)

    def save(self) -> None:
        self.saved = True


@pytest.fixture
def fake_entry(monkeypatch: pytest.MonkeyPatch) -> type[_FakeEntry]:
    _FakeEntry.instances = []
    monkeypatch.setattr(extensions, "RedBeatSchedulerEntry", _FakeEntry)
    return _FakeEntry


async def test_schedule_task_saves_interval_entry(fake_entry) -> None:
    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    params = inspect.signature(branch).parameters
    assert {"backend_schedule_name", "backend_schedule"} <= set(params)

    await branch(a=1, backend_schedule_name="nightly", backend_schedule=30)
    (entry,) = fake_entry.instances
    assert entry.saved
    assert entry.kwargs["name"] == "nightly"
    assert entry.kwargs["task"] == "celery.tool_execution"
    assert isinstance(entry.kwargs["schedule"], schedule)
    assert entry.kwargs["schedule"].run_every.total_seconds() == 30.0
    assert entry.kwargs["kwargs"] == {
        "a": 1,
        "b": "x",
        "backend_tool_name": "sample_tool",
        "backend_secret_capability": False,
    }


async def test_schedule_task_saves_crontab_entry(fake_entry) -> None:
    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    await branch(a=1, backend_schedule_name="weekday", backend_schedule="0 9 * * 1")
    (entry,) = fake_entry.instances
    assert isinstance(entry.kwargs["schedule"], crontab)


async def test_schedule_task_requires_name_and_schedule(fake_entry) -> None:
    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    with pytest.raises(ValueError, match="backend_schedule_name is required"):
        await branch(a=1, backend_schedule=30)
    with pytest.raises(ValueError, match="backend_schedule is required"):
        await branch(a=1, backend_schedule_name="nightly")
    assert fake_entry.instances == []


async def test_schedule_task_rejects_an_unknown_normalized_kind(fake_entry, monkeypatch) -> None:
    """Defensive guard behind normalize_schedule: an unexpected kind raises."""
    monkeypatch.setattr(extensions, "normalize_schedule", lambda s: {"__type__": "hourly"})
    branch = extensions.schedule_task(sample_tool, "sample_tool", "doc")
    with pytest.raises(ValueError, match="Unsupported schedule type"):
        await branch(a=1, backend_schedule_name="nightly", backend_schedule=30)
    assert fake_entry.instances == []
