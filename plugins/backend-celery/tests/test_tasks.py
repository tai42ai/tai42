"""Task plumbing: the per-task loop, the stream-capture guard, and run_tool."""

from __future__ import annotations

import sys

import pytest

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
    out = await run_tool(backend_tool_name="my_tool", a=1)
    assert out == "result"
    assert stub_app.tools.run_tool_calls == [("my_tool", {"a": 1})]


async def test_run_tool_missing_tool_name_raises(stub_app) -> None:
    with pytest.raises(KeyError):
        await run_tool(a=1)


def test_tool_execution_runs_tool_through_app(stub_app) -> None:
    stub_app.tools.run_tool_result = {"ok": True}
    try:
        out = tool_execution.apply(kwargs={"backend_tool_name": "my_tool", "x": 2}).get()
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
    from tai42_backend_celery.core.callback import CallbackSchema
    from tai42_backend_celery.core.tasks import callback_task

    try:
        out = callback_task.apply(args=({"k": 3}, CallbackSchema(expr=".k * 2"))).get()
    finally:
        if callback_task._loop is not None:
            callback_task._loop.close()
            callback_task._loop = None
    assert out == 6
