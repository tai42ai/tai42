"""``tool_execution`` / ``callback_job`` worker functions and the callback glue."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arq.jobs import JobStatus

from tai42_backend_arq import callback as callback_module
from tai42_backend_arq import tasks
from tai42_backend_arq.callback import CallbackSchema, callback_execution, prepare_backend_kwargs
from tai42_backend_arq.settings import ArqSettings


class _Ctx:
    def __init__(self) -> None:
        self.enqueued: list[tuple[Any, ...]] = []

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> Any:
        self.enqueued.append(args)
        return None


# -- tool_execution --------------------------------------------------------------------


async def test_tool_execution_runs_named_tool(stub_app) -> None:
    stub_app.tools.run_tool_mock = AsyncMock(return_value={"out": 1})
    ctx = {"redis": _Ctx(), "job_id": "job-9"}

    out = await tasks.tool_execution(ctx, backend_tool_name="mytool", text="hi")

    assert out == {"out": 1}
    stub_app.tools.run_tool_mock.assert_awaited_once_with("mytool", {"text": "hi"})


async def test_tool_execution_chains_callback_even_on_failure(stub_app) -> None:
    stub_app.tools.run_tool_mock = AsyncMock(side_effect=RuntimeError("tool blew up"))
    redis = _Ctx()
    ctx = {"redis": redis, "job_id": "job-9"}

    with pytest.raises(RuntimeError, match="tool blew up"):
        await tasks.tool_execution(ctx, backend_tool_name="mytool", callback_kwargs={"tool": "next"})

    assert redis.enqueued == [("callback_job", "job-9", {"tool": "next"})]


async def test_tool_execution_missing_tool_name_raises(stub_app) -> None:
    with pytest.raises(KeyError):
        await tasks.tool_execution({"redis": _Ctx(), "job_id": "j"}, text="hi")


# -- callback_job ------------------------------------------------------------------------


class _FakeJob:
    def __init__(self, *, statuses: list[Any], result: Any) -> None:
        self._statuses = list(statuses)
        self._result = result

    async def status(self) -> Any:
        value = self._statuses.pop(0) if len(self._statuses) > 1 else self._statuses[0]
        if isinstance(value, Exception):
            raise value
        return value

    async def result(self, timeout: float | None = None) -> Any:
        if isinstance(self._result, BaseException):
            raise self._result
        return self._result


def _bind_job(monkeypatch, job: _FakeJob, callback_timeout: int = 5) -> None:
    monkeypatch.setattr(tasks, "Job", lambda *a, **kw: job)
    monkeypatch.setattr(tasks, "arq_settings", lambda: ArqSettings(callback_timeout=callback_timeout))


async def test_callback_job_runs_callback_over_result(monkeypatch, stub_app) -> None:
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.complete], result={"value": 3}))
    stub_app.tools.run_tool_mock = AsyncMock(return_value="chained")

    out = await tasks.callback_job({"redis": object()}, "job-1", {"tool": "next_tool", "expr": "{v: .value}"})

    assert out == "chained"
    stub_app.tools.run_tool_mock.assert_awaited_once_with("next_tool", {"v": 3})


async def test_callback_job_not_found(monkeypatch) -> None:
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.not_found], result=None))
    out = await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())
    assert out == {"status": "error", "job_id": "job-1", "error": "Job not found"}


async def test_callback_job_timeout_reports_not_finished(monkeypatch) -> None:
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.in_progress], result=None), callback_timeout=0)
    out = await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())
    assert out["status"] == "not_finished"
    assert "did not complete within 0s" in out["error"]


async def test_callback_job_status_error_reported(monkeypatch) -> None:
    _bind_job(monkeypatch, _FakeJob(statuses=[ConnectionError("redis gone")], result=None))
    out = await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())
    assert out["status"] == "error"
    assert "redis gone" in out["error"]


async def test_callback_job_result_failure_reported(monkeypatch) -> None:
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.complete], result=ValueError("job failed")))
    out = await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())
    assert out["status"] == "failure"
    assert "job failed" in out["error"]


async def test_callback_job_aborted_predecessor_reported_as_failure(monkeypatch) -> None:
    """An aborted predecessor replays its stored abort as a revived
    ``CancelledError``; the callback job reports it as a failure with the
    stored detail instead of letting it read as a cancellation of the callback
    job itself."""
    _bind_job(monkeypatch, _FakeJob(statuses=[JobStatus.complete], result=asyncio.CancelledError("CancelledError()")))
    out = await tasks.callback_job({"redis": object()}, "job-1", CallbackSchema())
    assert out["status"] == "failure"
    assert "CancelledError()" in out["error"]


# -- callback_execution -------------------------------------------------------------------


async def test_callback_condition_pass_runs_tool(stub_app) -> None:
    stub_app.tools.run_tool_mock = AsyncMock(return_value="ran")
    cb = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")

    out = await callback_execution({"ok": True, "value": 5}, cb)

    assert out == "ran"
    stub_app.tools.run_tool_mock.assert_awaited_once_with("next", {"x": 5})


async def test_callback_condition_fail_returns_none(stub_app) -> None:
    cb = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")
    out = await callback_execution({"ok": False, "value": 5}, cb)
    assert out is None
    stub_app.tools.run_tool_mock.assert_not_called()


async def test_callback_without_tool_returns_expr_output() -> None:
    cb = CallbackSchema(expr="{doubled: (.value * 2)}")
    out = await callback_execution({"value": 4}, cb)
    assert out == {"doubled": 8}


async def test_callback_without_expr_yields_empty_kwargs(stub_app) -> None:
    stub_app.tools.run_tool_mock = AsyncMock(return_value="ran")
    cb = CallbackSchema(tool="next")
    out = await callback_execution({"value": 4}, cb)
    assert out == "ran"
    stub_app.tools.run_tool_mock.assert_awaited_once_with("next", {})


# -- prepare_backend_kwargs / render methods -----------------------------------------------


async def test_prepare_backend_kwargs_injects_tool_name() -> None:
    async def tool(a: int) -> int:
        return a

    out = await prepare_backend_kwargs(tool, "backend_tool_name", "tool", {"a": 1})
    assert out == {"a": 1, "backend_tool_name": "tool"}


async def test_rendered_fields_resolve_through_resource_manager() -> None:
    cb = CallbackSchema(condition=".ok", expr=".x")
    assert await cb.rendered_condition() == ".ok"
    assert await cb.rendered_expr() == ".x"
    empty = CallbackSchema()
    assert await empty.rendered_condition() == ""
    assert await empty.rendered_expr() == ""


def test_callback_module_exports() -> None:
    assert callback_module.CallbackSchema is CallbackSchema
