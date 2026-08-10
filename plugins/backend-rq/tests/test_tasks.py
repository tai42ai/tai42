"""The job functions and the enqueue option mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from rq.exceptions import NoSuchJobError

from tai42_backend_rq import tasks
from tai42_backend_rq.callback import CallbackSchema
from tai42_backend_rq.settings import rq_settings

from .conftest import make_client_ctx

# --- tool_execution ----------------------------------------------------------


async def test_tool_execution_dispatches_and_closes_clients(app, monkeypatch):
    closed: list[bool] = []

    async def fake_shutdown() -> None:
        closed.append(True)

    monkeypatch.setattr(tasks, "shutdown_all_clients", fake_shutdown)
    app.tools.run_result = {"out": 1}

    arg_name = rq_settings().tool_name_arg
    result = await tasks.tool_execution(**{arg_name: "my_tool", "x": 5})

    assert result == {"out": 1}
    assert app.tools.run_calls == [("my_tool", {"x": 5})]
    assert closed == [True]


async def test_tool_execution_missing_tool_name_raises_but_still_cleans_up(app, monkeypatch):
    closed: list[bool] = []

    async def fake_shutdown() -> None:
        closed.append(True)

    monkeypatch.setattr(tasks, "shutdown_all_clients", fake_shutdown)

    with pytest.raises(KeyError):
        await tasks.tool_execution(x=5)
    # The kwargs were malformed before dispatch; no cleanup ran because no
    # clients could have been opened yet — the pop happens outside the try.
    assert closed == []


async def test_tool_execution_closes_clients_on_tool_failure(app, monkeypatch):
    closed: list[bool] = []

    async def fake_shutdown() -> None:
        closed.append(True)

    monkeypatch.setattr(tasks, "shutdown_all_clients", fake_shutdown)
    app.tools.run_error = RuntimeError("tool blew up")

    arg_name = rq_settings().tool_name_arg
    with pytest.raises(RuntimeError, match="tool blew up"):
        await tasks.tool_execution(**{arg_name: "my_tool"})
    assert closed == [True]


# --- callback_job ------------------------------------------------------------


class FakeResult:
    """Result-shaped: its str() is a repr, the failure text lives in exc_string."""

    def __init__(self, exc_string: str | None) -> None:
        if exc_string is not None:
            self.exc_string = exc_string

    def __repr__(self) -> str:
        return "Result(id=r-1, type=FAILED)"


class FakeFetchedJob:
    def __init__(
        self,
        *,
        failed: bool = False,
        finished: bool = True,
        value: Any = None,
        exc_string: str | None = "job failed hard",
    ) -> None:
        self.is_failed = failed
        self.is_finished = finished
        self._value = value
        self._exc_string = exc_string

    def latest_result(self) -> FakeResult:
        return FakeResult(self._exc_string)

    def return_value(self) -> Any:
        return self._value


def _patch_job_fetch(monkeypatch, job: FakeFetchedJob | Exception) -> None:
    class FakeJobClass:
        @staticmethod
        def fetch(job_id: str, connection: Any = None) -> FakeFetchedJob:
            if isinstance(job, Exception):
                raise job
            return job

    monkeypatch.setattr(tasks, "Job", FakeJobClass)
    monkeypatch.setattr(tasks, "client_ctx", make_client_ctx(object()))


async def test_callback_job_runs_callback_on_success(monkeypatch):
    _patch_job_fetch(monkeypatch, FakeFetchedJob(value={"n": 3}))
    seen: list[tuple[Any, Any]] = []

    async def fake_callback_execution(result: Any, callback: Any) -> str:
        seen.append((result, callback))
        return "chained"

    monkeypatch.setattr(tasks, "callback_execution", fake_callback_execution)

    callback = CallbackSchema(tool="next_tool")
    assert await tasks.callback_job("job-1", callback) == "chained"
    assert seen == [({"n": 3}, callback)]


async def test_callback_job_reports_failed_primary_with_exc_string(monkeypatch):
    """The reported error is the Result's persisted traceback text, not the
    Result object's repr."""
    _patch_job_fetch(monkeypatch, FakeFetchedJob(failed=True))
    result = await tasks.callback_job("job-1", CallbackSchema(tool="t"))
    assert result == {"status": "failure", "job_id": "job-1", "error": "job failed hard"}


async def test_callback_job_failed_primary_without_exc_string_falls_back(monkeypatch):
    _patch_job_fetch(monkeypatch, FakeFetchedJob(failed=True, exc_string=None))
    result = await tasks.callback_job("job-1", CallbackSchema(tool="t"))
    assert result == {"status": "failure", "job_id": "job-1", "error": "Result(id=r-1, type=FAILED)"}


async def test_callback_job_reports_unfinished_primary(monkeypatch):
    _patch_job_fetch(monkeypatch, FakeFetchedJob(finished=False))
    result = await tasks.callback_job("job-1", CallbackSchema(tool="t"))
    assert result == {"status": "not_finished", "job_id": "job-1", "error": "Job not completed"}


async def test_callback_job_missing_primary_raises(monkeypatch):
    _patch_job_fetch(monkeypatch, NoSuchJobError("gone"))
    with pytest.raises(NoSuchJobError):
        await tasks.callback_job("job-1", CallbackSchema(tool="t"))


async def test_callback_job_closes_clients_on_success(monkeypatch):
    """Like ``tool_execution``, the job runs on its own fresh event loop, so
    the pooled clients bound to that loop must be closed before it ends."""
    closed: list[bool] = []

    async def fake_shutdown() -> None:
        closed.append(True)

    monkeypatch.setattr(tasks, "shutdown_all_clients", fake_shutdown)
    _patch_job_fetch(monkeypatch, FakeFetchedJob(value={"n": 1}))

    async def fake_callback_execution(result: Any, callback: Any) -> str:
        return "chained"

    monkeypatch.setattr(tasks, "callback_execution", fake_callback_execution)

    assert await tasks.callback_job("job-1", CallbackSchema(tool="t")) == "chained"
    assert closed == [True]


async def test_callback_job_closes_clients_when_it_raises(monkeypatch):
    closed: list[bool] = []

    async def fake_shutdown() -> None:
        closed.append(True)

    monkeypatch.setattr(tasks, "shutdown_all_clients", fake_shutdown)
    _patch_job_fetch(monkeypatch, NoSuchJobError("gone"))

    with pytest.raises(NoSuchJobError):
        await tasks.callback_job("job-1", CallbackSchema(tool="t"))
    assert closed == [True]


# --- enqueue_task ------------------------------------------------------------


class FakeEnqueuedJob:
    def __init__(self, job_id: str = "job-1") -> None:
        self.id = job_id


class FakeQueue:
    def __init__(self, connection: Any = None) -> None:
        self.calls: list[tuple[str, Any]] = []

    def enqueue(self, func: Any, *args: Any, **kwargs: Any) -> FakeEnqueuedJob:
        self.calls.append(("enqueue", (func, args, kwargs)))
        return FakeEnqueuedJob(f"job-{len(self.calls)}")

    def enqueue_at(self, when: datetime, func: Any, *args: Any, **kwargs: Any) -> FakeEnqueuedJob:
        self.calls.append(("enqueue_at", (when, func, args, kwargs)))
        return FakeEnqueuedJob()

    def enqueue_in(self, delta: timedelta, func: Any, *args: Any, **kwargs: Any) -> FakeEnqueuedJob:
        self.calls.append(("enqueue_in", (delta, func, args, kwargs)))
        return FakeEnqueuedJob()


@pytest.fixture
def queue(monkeypatch) -> FakeQueue:
    fake_queue = FakeQueue()
    monkeypatch.setattr(tasks, "Queue", lambda connection=None: fake_queue)
    monkeypatch.setattr(tasks, "client_ctx", make_client_ctx(object()))
    return fake_queue


async def test_enqueue_task_plain(queue):
    job = await tasks.enqueue_task(a=1)
    assert job.id == "job-1"
    [(kind, (func, _args, opts))] = queue.calls
    assert kind == "enqueue"
    assert func is tasks.tool_execution
    assert opts == {"args": (), "kwargs": {"a": 1}, "ttl": None}


async def test_enqueue_task_passes_tool_kwargs_explicitly(queue):
    """Tool kwargs travel in the explicit ``kwargs=`` slot: a name colliding
    with an rq job option (``description``/``meta``/...) must reach the tool,
    not be consumed as a job option."""
    await tasks.enqueue_task(description="user text", meta={"m": 1})
    [(_kind, (_func, _args, opts))] = queue.calls
    assert opts["kwargs"] == {"description": "user text", "meta": {"m": 1}}


async def test_enqueue_task_eta_maps_to_enqueue_at(queue):
    eta = datetime(2030, 1, 1, tzinfo=UTC)
    await tasks.enqueue_task(a=1, eta=eta.isoformat(), expires=90.5)
    [(kind, (when, _func, _args, opts))] = queue.calls
    assert kind == "enqueue_at"
    assert when == eta
    assert opts == {"args": (), "kwargs": {"a": 1}, "ttl": 90}


async def test_enqueue_task_countdown_maps_to_enqueue_in(queue):
    await tasks.enqueue_task(a=1, countdown=30)
    [(kind, (delta, _func, _args, _opts))] = queue.calls
    assert kind == "enqueue_in"
    assert delta == timedelta(seconds=30)


async def test_enqueue_task_chains_callback_job(queue):
    callback = CallbackSchema(tool="next")
    job = await tasks.enqueue_task(a=1, callback_kwargs=callback)

    assert [kind for kind, _ in queue.calls] == ["enqueue", "enqueue"]
    _, (func, args, opts) = queue.calls[1]
    assert func is tasks.callback_job
    assert args == ()
    assert opts == {"args": (job.id, callback), "depends_on": job}


async def test_enqueue_task_none_options_are_dropped(queue):
    await tasks.enqueue_task(a=1, countdown=None, eta=None, expires=None, callback_kwargs=None)
    [(kind, _)] = queue.calls
    assert kind == "enqueue"
