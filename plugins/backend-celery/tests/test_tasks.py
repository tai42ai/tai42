"""Task plumbing: the per-task loop, the stream-capture guard, and run_tool."""

from __future__ import annotations

import sys
from typing import Any

import pytest
from tai42_contract.access_control import caller_may_read_secrets
from tai42_contract.template import TemplatedText
from tai42_kit.utils.detached_util import in_detached_run

from tai42_backend_celery.core import tasks as tasks_module
from tai42_backend_celery.core.tasks import AsyncTask, prevent_celery_stream_capture, run_tool, tool_execution


def test_prevent_stream_capture_restores_real_fds_then_proxies() -> None:
    class _Proxy:
        pass

    proxy_out, proxy_err = _Proxy(), _Proxy()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = proxy_out, proxy_err  # type: ignore[assignment]
    try:
        with prevent_celery_stream_capture():
            assert sys.stdout is sys.__stdout__
            assert sys.stderr is sys.__stderr__
        assert sys.stdout is proxy_out
        assert sys.stderr is proxy_err
    finally:
        sys.stdout, sys.stderr = old_out, old_err


def test_async_task_loop_is_cached_and_rebuilt_after_close(stub_app) -> None:
    task = AsyncTask()
    loop = task.loop
    assert task.loop is loop
    task.close_loop()
    assert loop.is_closed()
    assert stub_app.clients.shutdown_calls == 1
    rebuilt = task.loop
    assert rebuilt is not loop
    assert not rebuilt.is_closed()
    rebuilt.close()


def test_close_loop_without_loop_is_a_noop(stub_app) -> None:
    task = AsyncTask()
    task.close_loop()
    assert stub_app.clients.shutdown_calls == 0


def test_run_async_runs_on_the_task_loop() -> None:
    task = AsyncTask()

    async def _coro() -> int:
        return 41

    try:
        assert task.run_async(_coro()) == 41
    finally:
        task._loop.close()  # type: ignore[union-attr]


async def test_run_tool_pops_tool_name_arg(stub_app) -> None:
    stub_app.tools.run_tool_result = "result"
    out = await run_tool(backend_tool_name="my_tool", backend_secret_capability=False, a=1)
    assert out == "result"
    assert stub_app.tools.run_tool_calls == [("my_tool", {"a": 1})]


async def test_run_tool_missing_tool_name_raises(stub_app) -> None:
    with pytest.raises(KeyError):
        await run_tool(a=1)


async def test_run_tool_runs_the_tool_detached(stub_app) -> None:
    # A worker execution has no live caller, so the tool observes the detached
    # flag set; the flag never leaks past the run.
    await run_tool(backend_tool_name="my_tool", backend_secret_capability=False, a=1)
    assert stub_app.tools.detached_seen == [True]
    assert stub_app.tools.offloads == [True]
    assert in_detached_run() is False


@pytest.mark.parametrize("capability", [True, False])
async def test_run_tool_binds_the_carried_capability(stub_app, capability: bool) -> None:
    # The worker binds the capability the job carried verbatim, never passes the reserved
    # kwarg to the tool, and the bind never leaks past the run.
    stub_app.tools.run_tool_result = "result"
    out = await run_tool(backend_tool_name="my_tool", backend_secret_capability=capability, a=1)
    assert out == "result"
    assert stub_app.tools.secret_capability_seen == [capability]
    assert stub_app.tools.run_tool_calls == [("my_tool", {"a": 1})]
    assert caller_may_read_secrets() is False


async def test_run_tool_without_a_carried_capability_raises(stub_app) -> None:
    with pytest.raises(KeyError, match="backend_secret_capability"):
        await run_tool(backend_tool_name="my_tool", a=1)
    assert stub_app.tools.run_tool_calls == []


def test_tool_execution_runs_tool_through_app(stub_app) -> None:
    stub_app.tools.run_tool_result = {"ok": True}
    try:
        out = tool_execution.apply(
            kwargs={"backend_tool_name": "my_tool", "backend_secret_capability": False, "x": 2}
        ).get()
    finally:
        if tool_execution._loop is not None:  # close the task loop opened by apply()
            tool_execution._loop.close()
            tool_execution._loop = None
    assert out == {"ok": True}
    assert stub_app.tools.run_tool_calls == [("my_tool", {"x": 2})]


def test_task_opts_expose_callback_kwargs() -> None:
    assert set(tasks_module.CELERY_TASK_OPTS) == {
        "queue",
        "countdown",
        "priority",
        "retry",
        "routing_key",
        "expires",
        "eta",
        "callback_kwargs",
    }
    assert set(tasks_module.CELERY_SCHEDULE_OPTS) == {"backend_schedule_name", "backend_schedule"}


def test_tool_execution_retry_policy() -> None:
    assert ConnectionError in tool_execution.autoretry_for
    assert TimeoutError in tool_execution.autoretry_for
    assert ValueError in tool_execution.dont_autoretry_for
    assert tool_execution.max_retries == 5
    assert tool_execution.retry_backoff is True
    assert tool_execution.retry_backoff_max == 600
    assert tool_execution.retry_jitter is True


def test_callback_task_runs_callback_execution(stub_app) -> None:
    from tai42_kit.backend import CallbackSchema

    from tai42_backend_celery.core.tasks import callback_task

    try:
        out = callback_task.apply(args=({"k": 3}, CallbackSchema(expr=TemplatedText(content=".k * 2")))).get()
    finally:
        if callback_task._loop is not None:
            callback_task._loop.close()
            callback_task._loop = None
    assert out == 6


@pytest.mark.parametrize("capability", [True, False])
def test_callback_task_retried_after_a_transient_error_runs_with_the_carried_capability(
    stub_app, monkeypatch: pytest.MonkeyPatch, capability: bool
) -> None:
    # celery's autoretry re-sends the task's own argument list, so the retry runs the SAME
    # spec object the first attempt was handed.
    from tai42_kit.backend import CallbackSchema
    from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

    from tai42_backend_celery.core.tasks import callback_task

    recorded_run_tool = stub_app.tools.run_tool
    attempts: list[bool] = []

    async def fails_once_then_runs(key: str, arguments: Any, **kwargs: Any) -> Any:
        attempts.append(caller_may_read_secrets())
        if len(attempts) == 1:
            raise ConnectionError("transient broker error")
        return await recorded_run_tool(key, arguments, **kwargs)

    monkeypatch.setattr(stub_app.tools, "run_tool", fails_once_then_runs)
    monkeypatch.setattr(stub_app.tools, "run_tool_result", {"ran": True})
    callback = CallbackSchema(
        expr=TemplatedText(content="."),
        tool="follow_up",
        carried_kwargs={WORKER_SECRET_CAPABILITY_ARG: capability},
    )
    try:
        outcome = callback_task.apply(args=({"k": 1}, callback))
    finally:
        if callback_task._loop is not None:
            callback_task._loop.close()
            callback_task._loop = None
    assert outcome.state == "SUCCESS", outcome.result
    assert outcome.result == {"ran": True}
    assert attempts == [capability, capability]
