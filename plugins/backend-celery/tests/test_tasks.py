"""Task plumbing: the per-task loop, the stream-capture guard, and run_tool."""

from __future__ import annotations

import asyncio
import sys
from typing import Any

import pytest
from tai42_contract.access_control import caller_may_read_secrets
from tai42_contract.backend import SCHEDULE_CADENCE_KEY, SCHEDULE_NAME_KEY
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


def test_a_free_loop_serves_the_next_job_and_close_shuts_it_down(stub_app) -> None:
    task = AsyncTask()
    with task.loops.take() as first:
        pass
    with task.loops.take() as second:
        assert asyncio.get_event_loop() is second
    assert second is first

    task.close_loop()

    assert first.is_closed()
    assert stub_app.clients.shutdown_calls == 1
    with task.loops.take() as rebuilt:
        assert rebuilt is not first
        assert not rebuilt.is_closed()
    task.close_loop()


def test_jobs_at_once_each_hold_a_loop_of_their_own(stub_app) -> None:
    task = AsyncTask()
    with task.loops.take() as first, task.loops.take() as second:
        assert second is not first
    with task.loops.take() as third:
        assert third in (first, second)

    assert task.loops.count == 2
    task.close_loop()
    assert first.is_closed()
    assert second.is_closed()
    assert stub_app.clients.shutdown_calls == 2


def test_a_closed_free_loop_is_replaced(stub_app) -> None:
    task = AsyncTask()
    with task.loops.take() as first:
        pass
    first.close()

    with task.loops.take() as replaced:
        assert replaced is not first
        assert not replaced.is_closed()

    assert task.loops.count == 1
    task.close_loop()


def test_close_closes_every_loop_and_raises_each_failed_shutdown(stub_app, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fail() -> None:
        stub_app.clients.shutdown_calls += 1
        raise RuntimeError("pool close failed")

    task = AsyncTask()
    with task.loops.take() as first, task.loops.take() as second:
        pass
    monkeypatch.setattr(stub_app.clients, "shutdown_clients", _fail)

    with pytest.raises(ExceptionGroup) as raised:
        task.close_loop()

    assert [str(failure) for failure in raised.value.exceptions] == ["pool close failed", "pool close failed"]
    assert first.is_closed()
    assert second.is_closed()
    assert task.loops.count == 0


def test_close_loop_without_loop_is_a_noop(stub_app) -> None:
    task = AsyncTask()
    task.close_loop()
    assert stub_app.clients.shutdown_calls == 0


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
        tool_execution.close_loop()  # close the task loop opened by apply()
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
    assert set(tasks_module.CELERY_SCHEDULE_OPTS) == {SCHEDULE_NAME_KEY, SCHEDULE_CADENCE_KEY}


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


def test_run_async_counts_the_job_in_the_worker_s_job_slots_and_holds_the_fork_gate() -> None:
    """On a pool that cannot end a job, every task body passes through the slot accounting and the fork gate."""
    from tai42_kit.fork_gate import fork_gate

    from tai42_backend_celery.core import job_slots

    job_slots.watch(pool_name="solo", in_process=True, budget=0.0, ticks_in_job=False, tick=lambda: None)

    async def _coro() -> int:
        await asyncio.sleep(0.01)
        with pytest.raises(RuntimeError, match=rf"on the solo pool, .*: {tool_execution.name} req-1$"):
            job_slots.check_wedged()
        assert fork_gate.live_spans == 1, "the in-process job does not hold the fork gate"
        return 41

    tool_execution.push_request(id="req-1")
    try:
        assert tool_execution.run_async(_coro()) == 41
    finally:
        tool_execution.pop_request()
        tool_execution.close_loop()

    job_slots.check_wedged()
    assert fork_gate.live_spans == 0
