"""Task plumbing: the per-task loops, the stream-capture guard, and run_tool."""

from __future__ import annotations

import asyncio
import inspect
import sys
from typing import Any

import pytest
from tai42_contract.access_control import caller_may_read_secrets
from tai42_contract.template import TemplatedText
from tai42_kit.backend import SharedLoop

from tai42_backend_celery.core import tasks as tasks_module
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.task_loops import PrivateLoops
from tai42_backend_celery.core.tasks import (
    AsyncTask,
    callback_task,
    prevent_celery_stream_capture,
    run_tool,
    tool_execution,
)


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


def test_close_loop_without_loop_is_a_noop(loop_releases) -> None:
    task = AsyncTask()
    task.close_loop()
    assert loop_releases.calls == 0


def test_with_no_pool_watched_a_task_runs_its_jobs_on_private_loops(stub_app) -> None:
    task = AsyncTask()

    assert isinstance(task.loops, PrivateLoops)


def _watch_a_green_pool() -> None:
    from tai42_backend_celery.core import job_slots

    job_slots.watch(
        celery_app, pool_name="gevent", in_process=True, green=True, budget=300.0, ticks_in_job=False, tick=lambda: None
    )


def test_on_a_pool_that_runs_a_job_per_os_thread_a_task_runs_its_jobs_on_private_loops(
    stub_app, host_loop: asyncio.AbstractEventLoop
) -> None:
    from tai42_backend_celery.core import job_slots

    job_slots.watch(
        celery_app,
        pool_name="threads",
        in_process=True,
        green=False,
        budget=300.0,
        ticks_in_job=False,
        tick=lambda: None,
    )
    AsyncTask.share_loop(celery_app, host_loop)
    task = AsyncTask()

    try:
        assert isinstance(task.loops, PrivateLoops)
    finally:
        task.close_loop()


def test_on_a_green_pool_a_task_runs_its_jobs_on_the_loop_handed_to_them(
    stub_app, host_loop: asyncio.AbstractEventLoop
) -> None:
    _watch_a_green_pool()
    AsyncTask.share_loop(celery_app, host_loop)

    assert isinstance(AsyncTask().loops, SharedLoop)


def test_on_a_green_pool_with_no_loop_handed_a_task_s_jobs_are_refused(stub_app) -> None:
    _watch_a_green_pool()

    with pytest.raises(
        RuntimeError,
        match=r"^a green pool's jobs run on the host's event loop, and none was handed to them; the launch hands it "
        r"with AsyncTask\.share_loop before the worker starts$",
    ):
        _ = tool_execution.loops


def test_on_a_green_pool_every_task_s_jobs_run_on_the_host_s_loop(
    stub_app, host_loop: asyncio.AbstractEventLoop
) -> None:
    _watch_a_green_pool()
    AsyncTask.share_loop(celery_app, host_loop)

    async def _on_the_host_s_loop() -> bool:
        return asyncio.get_running_loop() is host_loop

    assert [task.run_async(_on_the_host_s_loop()) for task in (tool_execution, callback_task)] == [True, True]


def test_letting_the_shared_loop_go_refuses_every_task_s_later_job_and_leaves_the_host_s_loop_and_clients(
    loop_releases, host_loop: asyncio.AbstractEventLoop
) -> None:
    _watch_a_green_pool()
    AsyncTask.share_loop(celery_app, host_loop)
    assert tool_execution.run_async(asyncio.sleep(0, result=1)) == 1

    AsyncTask.close_shared_loop(celery_app)
    AsyncTask.close_shared_loop(celery_app)

    for task in (tool_execution, callback_task):
        with pytest.raises(RuntimeError, match=r"^the jobs' shared event loop was let go; it runs no further job$"):
            task.run_async(asyncio.sleep(0, result=1))
    assert asyncio.run_coroutine_threadsafe(asyncio.sleep(0, result=2), host_loop).result(5) == 2
    assert loop_releases.calls == 0


@pytest.mark.parametrize("handed", ["no loop", "a loop let go"])
def test_a_job_refused_before_its_coroutine_started_closes_the_coroutine(
    stub_app, host_loop: asyncio.AbstractEventLoop, handed: str
) -> None:
    _watch_a_green_pool()
    if handed == "a loop let go":
        AsyncTask.share_loop(celery_app, host_loop)
        AsyncTask.close_shared_loop(celery_app)
    coroutine = asyncio.sleep(0, result=1)

    with pytest.raises(RuntimeError):
        tool_execution.run_async(coroutine)

    assert inspect.getcoroutinestate(coroutine) == inspect.CORO_CLOSED


def test_closing_a_task_s_loops_leaves_the_shared_loop_to_the_other_tasks(
    stub_app, host_loop: asyncio.AbstractEventLoop
) -> None:
    _watch_a_green_pool()
    AsyncTask.share_loop(celery_app, host_loop)
    shared = tool_execution.loops

    tool_execution.close_loop()

    assert callback_task.loops is shared
    assert callback_task.run_async(asyncio.sleep(0, result=1)) == 1


def test_close_loop_drops_the_loops_so_the_next_job_makes_them_anew(stub_app) -> None:
    task = AsyncTask()
    closed = task.loops

    task.close_loop()

    assert task.loops is not closed
    task.close_loop()


def test_run_async_runs_on_the_task_loop() -> None:
    async def _coro() -> int:
        return 41

    try:
        assert tool_execution.run_async(_coro()) == 41
    finally:
        tool_execution.close_loop()


async def test_run_tool_pops_tool_name_arg(stub_app) -> None:
    stub_app.tools.run_tool_result = "result"
    out = await run_tool(backend_tool_name="my_tool", backend_secret_capability=False, a=1)
    assert out == "result"
    assert stub_app.tools.run_tool_calls == [("my_tool", {"a": 1})]


def test_tool_execution_runs_tool_through_app(stub_app) -> None:
    stub_app.tools.run_tool_result = {"ok": True}
    try:
        out = tool_execution.apply(
            kwargs={"backend_tool_name": "my_tool", "backend_secret_capability": False, "x": 2}
        ).get()
    finally:
        tool_execution.close_loop()  # close the task loop opened by apply()
    assert out == {"ok": True}
    assert stub_app.tools.run_tool_calls == [("my_tool", {"x": 2})]


def test_neither_task_carries_an_autoretry_wrapper() -> None:
    from tai42_backend_celery.core.tasks import callback_task

    for task in (tool_execution, callback_task):
        assert not hasattr(task, "_orig_run")
        assert not getattr(task, "autoretry_for", ())


def test_a_tool_body_raising_timeout_error_runs_once(monkeypatch: pytest.MonkeyPatch, stub_app) -> None:
    # Builtin ``TimeoutError`` (and ``ConnectionError``) from a tool body is the tool's failure: the
    # task never re-fires the body; retries are declared per tool and applied by the platform.
    calls: list[dict[str, Any]] = []

    async def _boom(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise TimeoutError("upstream too slow")

    monkeypatch.setattr(tasks_module, "run_tool", _boom)
    try:
        result = tool_execution.apply(kwargs={"x": 1})
    finally:
        tool_execution.close_loop()  # close the task loop opened by apply()
    assert calls == [{"x": 1}]
    assert isinstance(result.result, TimeoutError)


def test_callback_task_runs_callback_execution(stub_app) -> None:
    from tai42_kit.backend import CallbackSchema

    from tai42_backend_celery.core.tasks import callback_task

    try:
        out = callback_task.apply(args=({"k": 3}, CallbackSchema(expr=TemplatedText(content=".k * 2")))).get()
    finally:
        callback_task.close_loop()
    assert out == 6


@pytest.mark.parametrize("capability", [True, False])
def test_callback_task_with_a_transient_error_runs_once_with_the_carried_capability(
    stub_app, monkeypatch: pytest.MonkeyPatch, capability: bool
) -> None:
    # A callback's builtin ``ConnectionError`` is its failure: the task never re-fires it, and the
    # one attempt runs under the capability the spec carried.
    from tai42_kit.backend import CallbackSchema
    from tai42_kit.utils.worker_secret_capability import WORKER_SECRET_CAPABILITY_ARG

    from tai42_backend_celery.core.tasks import callback_task

    recorded_run_tool = stub_app.tools.run_tool
    attempts: list[bool] = []

    async def fails_once_then_runs(key: str, arguments: Any, **kwargs: Any) -> Any:
        attempts.append(caller_may_read_secrets())
        if len(attempts) == 1:
            raise ConnectionError("transient broker error")
        return await recorded_run_tool(key, arguments, **kwargs)  # pragma: no cover - never re-run

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
        callback_task.close_loop()
    assert outcome.state == "FAILURE", outcome.result
    assert isinstance(outcome.result, ConnectionError)
    assert attempts == [capability]


def test_run_async_counts_the_job_in_the_worker_s_job_slots_and_holds_the_fork_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On an in-process pool, every task body passes through the slot accounting and the fork gate."""
    from tai42_kit.fork_gate import fork_gate

    from tai42_backend_celery.core import job_slots

    monkeypatch.setattr(job_slots, "PROGRESS_TICK_SECONDS", 0.01)
    job_slots.watch(
        celery_app, pool_name="solo", in_process=True, green=False, budget=0.0, ticks_in_job=False, tick=lambda: None
    )

    async def _coro() -> int:
        await asyncio.sleep(0.05)
        with pytest.raises(RuntimeError, match=rf"on the solo pool, .*: {tool_execution.name} req-1$"):
            job_slots.check_wedged(celery_app)
        assert fork_gate.live_spans == 1, "the in-process job does not hold the fork gate"
        return 41

    tool_execution.push_request(id="req-1")
    try:
        assert tool_execution.run_async(_coro()) == 41
    finally:
        tool_execution.pop_request()
        tool_execution.close_loop()

    job_slots.check_wedged(celery_app)
    assert fork_gate.live_spans == 0
