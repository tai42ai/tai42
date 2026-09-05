"""``tool_execution`` / ``callback_job`` worker functions and the callback glue."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arq.jobs import JobStatus
from tai42_contract.access_control import caller_may_read_secrets
from tai42_kit.backend import CallbackSchema, callback_execution, prepare_backend_kwargs
from tai42_kit.settings.cache_registry import reset_all_settings
from tai42_kit.utils.data import jq_util
from tai42_kit.utils.detached_util import in_detached_run

from tai42_backend_arq import tasks
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


async def test_tool_execution_runs_the_tool_detached(stub_app) -> None:
    # A worker execution has no live caller, so the tool observes the detached
    # flag set; the flag never leaks past the job.
    ctx = {"redis": _Ctx(), "job_id": "job-9"}

    await tasks.tool_execution(ctx, backend_tool_name="mytool", text="hi")

    assert stub_app.tools.detached_seen == [True]
    assert stub_app.tools.offloads == [True]
    assert in_detached_run() is False


@pytest.mark.parametrize(("gate_enabled", "capable"), [(False, True), (True, False)])
async def test_tool_execution_binds_the_worker_secret_capability(
    stub_app, access_control, gate_enabled: bool, capable: bool
) -> None:
    # No HTTP request bound the capability, so the worker binds it to the gate
    # state (OFF -> secret-capable synthetic admin, ON -> fail-closed) and the
    # bind never leaks past the job.
    access_control(gate_enabled)
    ctx = {"redis": _Ctx(), "job_id": "job-9"}

    await tasks.tool_execution(ctx, backend_tool_name="mytool", text="hi")

    assert stub_app.tools.secret_capability_seen == [capable]
    assert caller_may_read_secrets() is False


async def test_tool_execution_binds_the_propagated_submitter_capability(stub_app, access_control) -> None:
    # An admin submitter's capability rides with the job as True; the worker binds it
    # verbatim even gate ON, and never passes the reserved kwarg to the tool.
    access_control(True)
    ctx = {"redis": _Ctx(), "job_id": "job-9"}

    await tasks.tool_execution(ctx, backend_tool_name="mytool", backend_secret_capability=True, text="hi")

    assert stub_app.tools.secret_capability_seen == [True]
    stub_app.tools.run_tool_mock.assert_awaited_once_with("mytool", {"text": "hi"})
    assert caller_may_read_secrets() is False


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


async def test_callback_runs_tool_detached(stub_app) -> None:
    # A worker execution has no live caller, so the callback's tool observes the
    # detached flag set; the flag never leaks past the callback.
    cb = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")

    await callback_execution({"ok": True, "value": 5}, cb)

    assert stub_app.tools.detached_seen == [True]
    assert stub_app.tools.offloads == [True]
    assert in_detached_run() is False


@pytest.mark.parametrize(("gate_enabled", "capable"), [(False, True), (True, False)])
async def test_callback_binds_the_worker_secret_capability(
    stub_app, access_control, gate_enabled: bool, capable: bool
) -> None:
    # A dequeued callback's follow-up tool sees the same worker-bound capability as
    # a dequeued task: OFF -> secret-capable, ON -> fail-closed, reset after.
    access_control(gate_enabled)
    cb = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")

    await callback_execution({"ok": True, "value": 5}, cb)

    assert stub_app.tools.secret_capability_seen == [capable]
    assert caller_may_read_secrets() is False


async def test_callback_condition_fail_returns_none(stub_app) -> None:
    cb = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")
    out = await callback_execution({"ok": False, "value": 5}, cb)
    assert out is None
    stub_app.tools.run_tool_mock.assert_not_called()


async def test_callback_condition_empty_pipeline_skips(stub_app) -> None:
    # A condition that evaluates to an EMPTY pipeline (emits nothing) must skip the
    # callback (return None) rather than crash with the opaque RuntimeError.
    cb = CallbackSchema(condition=".errors[] | select(.fatal)", expr="{x: .value}", tool="next")
    out = await callback_execution({"errors": [{"fatal": False}], "value": 5}, cb)
    assert out is None
    stub_app.tools.run_tool_mock.assert_not_called()


async def test_callback_expr_empty_pipeline_yields_empty_mapping(stub_app) -> None:
    # An expr that evaluates to an EMPTY pipeline yields {} (default), passed to the
    # tool as {} — never the opaque RuntimeError.
    stub_app.tools.run_tool_mock = AsyncMock(return_value="ran")
    cb = CallbackSchema(condition=".ok", expr=".errors[] | select(.fatal)", tool="next")
    out = await callback_execution({"ok": True, "errors": [{"fatal": False}]}, cb)
    assert out == "ran"
    stub_app.tools.run_tool_mock.assert_awaited_once_with("next", {})


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


async def test_callback_jq_eval_is_timeout_bounded(stub_app, monkeypatch) -> None:
    # The callback path evaluates jq through ``run_jq_first``, so a slow program
    # is aborted by JQ_TIMEOUT_SECONDS and the named TimeoutError is raised.
    class _SlowProgram:
        def input(self, payload):
            return self

        def first(self):
            time.sleep(1)
            return None

    monkeypatch.setattr(jq_util, "get_compiled_jq", lambda expr, prelude="": _SlowProgram())
    monkeypatch.setenv("JQ_TIMEOUT_SECONDS", "0.01")
    reset_all_settings()
    try:
        cb = CallbackSchema(condition=".ok", expr="{x: .value}", tool="next")
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="JQ_TIMEOUT_SECONDS"):
            await callback_execution({"ok": True, "value": 5}, cb)
        assert time.monotonic() - start < 0.5
    finally:
        reset_all_settings()


# -- prepare_backend_kwargs / render methods -----------------------------------------------


async def test_prepare_backend_kwargs_injects_tool_name_and_stamps_capability() -> None:
    async def tool(a: int) -> int:
        return a

    # No request is bound in the test, so ``caller_may_read_secrets`` reads the fail-closed
    # default and the stamped capability is False; the worker pops it before running the tool.
    out = await prepare_backend_kwargs(tool, "backend_tool_name", "tool", {"a": 1})
    assert out == {"a": 1, "backend_tool_name": "tool", "backend_secret_capability": False}


async def test_rendered_fields_resolve_through_resource_manager() -> None:
    cb = CallbackSchema(condition=".ok", expr=".x")
    assert await cb.rendered_condition() == ".ok"
    assert await cb.rendered_expr() == ".x"
    empty = CallbackSchema()
    assert await empty.rendered_condition() == ""
    assert await empty.rendered_expr() == ""
