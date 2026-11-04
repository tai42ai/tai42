"""The Celery backend's task and worker tools as the registry serves them, over Celery's inspect API."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import pytest
from celery.exceptions import TimeoutError as CeleryTimeoutError
from tai42_kit.backend import UnsupportedBackendTool

from tai42_backend_celery.core import inspection


@pytest.fixture
def tools(stub_app: Any) -> SimpleNamespace:
    """The registered tools, by name."""
    return SimpleNamespace(**stub_app.tools.registered)


@pytest.mark.parametrize("name", ["backend_reserved_tasks", "backend_scheduled_tasks"])
def test_reserved_and_scheduled_reads_take_no_worker_name(stub_app, name):
    assert list(inspect.signature(stub_app.tools.registered[name]).parameters) == []


async def test_list_failed_tasks_is_unsupported(tools):
    with pytest.raises(UnsupportedBackendTool) as refused:
        await tools.backend_list_failed_tasks()
    assert str(refused.value) == "backend 'celery' does not support backend_list_failed_tasks"


# --- the worker views over celery inspect ----------------------------------


class _FakeInspect:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def _answer(self) -> Any:
        return self._payload

    ping = active = reserved = scheduled = registered = stats = active_queues = _answer


@pytest.fixture
def fake_inspect(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeInspect, list[Any]]:
    fake = _FakeInspect({"celery@a": {"ok": "pong"}})
    destinations: list[Any] = []

    def _inspect(destination: Any = None, timeout: float | None = None) -> _FakeInspect:
        assert timeout == inspection.REPLY_SECONDS
        destinations.append(destination)
        return fake

    monkeypatch.setattr(inspection, "celery_app", SimpleNamespace(control=SimpleNamespace(inspect=_inspect)))
    return fake, destinations


async def test_ping_and_list_active_workers(tools, fake_inspect):
    _fake, destinations = fake_inspect
    assert await tools.backend_ping_worker() == {"celery@a": {"ok": "pong"}}
    assert await tools.backend_ping_worker("celery@a") == {"celery@a": {"ok": "pong"}}
    assert await tools.backend_list_active_workers() == ["celery@a"]
    assert destinations == [None, ["celery@a"], None]


async def test_inspect_tools_return_empty_mappings_when_no_reply(tools, fake_inspect):
    fake_inspect[0]._payload = None
    assert await tools.backend_ping_worker() == {}
    assert await tools.backend_list_active_workers() == []
    assert await tools.backend_active_tasks() == {}
    assert await tools.backend_reserved_tasks() == {}
    assert await tools.backend_scheduled_tasks() == {}
    assert await tools.backend_registered_tasks() == {}
    assert await tools.backend_worker_stats() == {}
    assert await tools.backend_worker_queues() == {}


async def test_inspect_tools_pass_replies_through_to_the_named_worker(tools, fake_inspect):
    fake, destinations = fake_inspect
    fake._payload = {"celery@a": ["something"]}
    for name in ("backend_active_tasks", "backend_registered_tasks", "backend_worker_stats", "backend_worker_queues"):
        assert await getattr(tools, name)(worker_name="celery@a") == {"celery@a": ["something"]}
    assert await tools.backend_reserved_tasks() == {"celery@a": ["something"]}
    assert await tools.backend_scheduled_tasks() == {"celery@a": ["something"]}
    assert destinations == [["celery@a"]] * 4 + [None, None]


# --- task status, result and cancel over AsyncResult ------------------------


class _FakeAsyncResult:
    """Stand-in for celery's AsyncResult with controllable readiness."""

    def __init__(self, *, ready: bool, status: str, value: Any = None, failure: Exception | None = None) -> None:
        self._ready = ready
        self.status = status
        self._value = value
        self._failure = failure
        self.get_calls: list[float | None] = []

    def ready(self) -> bool:
        return self._ready

    def get(self, timeout: float | None = None) -> Any:
        self.get_calls.append(timeout)
        if self._failure is not None:
            raise self._failure
        return self._value

    @property
    def state(self) -> str:
        return self.status


def _patch_async_result(monkeypatch: pytest.MonkeyPatch, fake: _FakeAsyncResult) -> _FakeAsyncResult:
    monkeypatch.setattr(inspection, "AsyncResult", lambda task_id, app=None: fake)
    return fake


async def test_task_result_without_a_timeout_reads_the_state_at_once(tools, monkeypatch):
    fake = _patch_async_result(monkeypatch, _FakeAsyncResult(ready=False, status="PENDING"))
    assert await tools.backend_task_result("t1") == "Task t1 is not ready (status: PENDING)"
    assert fake.get_calls == []


async def test_task_result_without_a_timeout_returns_a_finished_result(tools, monkeypatch):
    fake = _patch_async_result(monkeypatch, _FakeAsyncResult(ready=True, status="SUCCESS", value=42))
    assert await tools.backend_task_result("t2") == 42
    assert fake.get_calls == [None]


async def test_task_result_with_a_timeout_waits(tools, monkeypatch):
    fake = _patch_async_result(monkeypatch, _FakeAsyncResult(ready=False, status="STARTED", value="done"))
    assert await tools.backend_task_result("t3", timeout=5) == "done"
    assert fake.get_calls == [5]


async def test_task_result_whose_timeout_elapses_is_not_ready(tools, monkeypatch):
    failure = CeleryTimeoutError("timed out")
    fake = _patch_async_result(monkeypatch, _FakeAsyncResult(ready=False, status="STARTED", failure=failure))
    assert await tools.backend_task_result("t4", timeout=1) == "Task t4 is not ready (status: STARTED)"
    assert fake.get_calls == [1]


async def test_task_result_raises_the_task_s_failure(tools, monkeypatch):
    _patch_async_result(monkeypatch, _FakeAsyncResult(ready=True, status="FAILURE", failure=ValueError("boom")))
    with pytest.raises(ValueError, match="boom"):
        await tools.backend_task_result("t5", timeout=1)


async def test_task_status_reads_the_result_backend_s_state(tools, monkeypatch):
    _patch_async_result(monkeypatch, _FakeAsyncResult(ready=False, status="STARTED"))
    assert await tools.backend_task_status("t1") == "STARTED"


# --- the cancel: a revoke awaited for the workers' replies ---------------------


class _FakeControl:
    """Stand-in for the app's control: its revoke broadcast returns the workers' replies when asked for them."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.revokes: list[tuple[str, dict[str, Any]]] = []

    def revoke(self, task_id: str, **kwargs: Any) -> list[dict[str, Any]] | None:
        self.revokes.append((task_id, kwargs))
        return self.replies if kwargs.get("reply") else None


class _StatesOverTime:
    """Stand-in for celery's AsyncResult class: each construction reads the next of ``states``, the last one after."""

    def __init__(self, states: list[str]) -> None:
        self._states = states
        self.constructions = 0

    def __call__(self, task_id: str, app: Any = None) -> _FakeAsyncResult:
        status = self._states[min(self.constructions, len(self._states) - 1)]
        self.constructions += 1
        return _FakeAsyncResult(ready=False, status=status)


def _revoking(
    monkeypatch: pytest.MonkeyPatch, status: str | list[str], replies: list[dict[str, Any]]
) -> tuple[_FakeControl, _StatesOverTime]:
    states = _StatesOverTime([status] if isinstance(status, str) else status)
    monkeypatch.setattr(inspection, "AsyncResult", states)
    control = _FakeControl(replies)
    monkeypatch.setattr(inspection, "celery_app", SimpleNamespace(control=control))
    return control, states


@pytest.mark.parametrize("terminate", [False, True])
async def test_cancel_awaits_the_workers_replies_for_the_fleet_view_s_timeout(tools, monkeypatch, terminate):
    control, _states = _revoking(monkeypatch, "PENDING", [{"celery@a": {"ok": "tasks ['t1'] flagged as revoked"}}])

    await tools.backend_cancel_task("t1", terminate=terminate)

    assert control.revokes == [("t1", {"terminate": terminate, "reply": True, "timeout": inspection.REPLY_SECONDS})]


async def test_cancel_reads_a_worker_s_revoke_as_revoked(tools, monkeypatch):
    _revoking(monkeypatch, "PENDING", [{"celery@a": {"ok": "tasks ['t1'] flagged as revoked"}}])
    assert await tools.backend_cancel_task("t1") == "Task t1 revoked"


async def test_cancel_with_terminate_reads_the_worker_that_ended_the_task_as_stopped(tools, monkeypatch):
    replies = [{"celery@a": {"ok": "terminate: tasks unknown"}}, {"celery@b": {"ok": "terminate: t1"}}]
    _revoking(monkeypatch, "STARTED", replies)
    assert await tools.backend_cancel_task("t1", terminate=True) == "Task t1 revoked and stopped"


@pytest.mark.parametrize("status", ["PENDING", "RETRY", "REVOKED"])
async def test_cancel_with_terminate_no_worker_confirms_is_not_confirmed(tools, monkeypatch, status):
    _revoking(monkeypatch, status, [{"celery@a": {"ok": "terminate: tasks unknown"}}])
    reply = await tools.backend_cancel_task("t1", terminate=True)
    assert reply == f"Task t1 stop requested but not confirmed (status: {status})"


@pytest.mark.parametrize("terminate", [False, True])
@pytest.mark.parametrize("status", ["SUCCESS", "FAILURE"])
async def test_cancel_of_an_ended_task_sends_no_revoke(tools, monkeypatch, status, terminate):
    control, _states = _revoking(monkeypatch, status, [{"celery@a": {"ok": "terminate: tasks unknown"}}])

    assert (
        await tools.backend_cancel_task("t1", terminate=terminate) == f"Task t1 cannot be canceled (status: {status})"
    )
    assert control.revokes == []


async def test_cancel_with_terminate_of_a_task_that_ended_under_the_stop(tools, monkeypatch):
    _revoking(monkeypatch, ["PENDING", "SUCCESS"], [{"celery@a": {"ok": "terminate: tasks unknown"}}])
    assert await tools.backend_cancel_task("t1", terminate=True) == "Task t1 cannot be stopped (status: SUCCESS)"


async def test_cancel_reads_the_state_afresh_after_the_broadcast(tools, monkeypatch):
    _control, states = _revoking(
        monkeypatch, ["PENDING", "REVOKED"], [{"celery@a": {"ok": "terminate: tasks unknown"}}]
    )

    reply = await tools.backend_cancel_task("t1", terminate=True)

    assert reply == "Task t1 stop requested but not confirmed (status: REVOKED)"
    assert states.constructions == 2


async def test_cancel_with_terminate_raises_the_refusal_of_a_worker_whose_pool_cannot_end_the_task(tools, monkeypatch):
    error = "NotImplementedError(\"<class 'celery.concurrency.thread.TaskPool'> does not implement kill_job\")"
    _revoking(
        monkeypatch, "STARTED", [{"celery@a": {"ok": "terminate: tasks unknown"}}, {"celery@b": {"error": error}}]
    )

    with pytest.raises(UnsupportedBackendTool) as refused:
        await tools.backend_cancel_task("t1", terminate=True)

    assert str(refused.value) == (
        "backend 'celery' does not support backend_cancel_task with terminate on worker 'celery@b', "
        f"whose pool cannot end a running task: {error}; the task is revoked and runs to its end"
    )


@pytest.mark.parametrize(
    ("terminate", "reply"),
    [
        (True, "Task t1 stop requested but not confirmed (status: PENDING)"),
        (False, "Task t1 revoke requested but not confirmed (status: PENDING)"),
    ],
)
async def test_cancel_no_worker_answers_is_not_confirmed(tools, monkeypatch, terminate, reply):
    _revoking(monkeypatch, "PENDING", [])
    assert await tools.backend_cancel_task("t1", terminate=terminate) == reply
