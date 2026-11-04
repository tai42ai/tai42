"""A reload body an in-process job drives from its own loop runs on the serving loop.

``reload_gate.run`` holds the reload lock and the fork exclusion on the caller's loop and in its
context, and awaits the body on the serving loop the built app has bound; the body's result,
its failure and its cancellation cross back to the caller. A cancelled caller keeps both holds
until the body has ended on the serving loop.
"""

from __future__ import annotations

import asyncio
import contextvars
import threading
from types import SimpleNamespace
from typing import Any

import pytest
from tai42_kit.fork_gate import fork_gate

from tai42_skeleton.app import instance
from tai42_skeleton.app.lifecycle import state
from tai42_skeleton.app.reload_gate import ReloadGate

from ..connectors.credential_path import job_loop

_REQUEST_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar("request_scope", default="<none>")


def test_the_body_runs_on_the_serving_loop_and_its_result_returns_to_the_job(serving) -> None:
    gate = ReloadGate()
    seen: dict[str, Any] = {}

    async def body() -> dict[str, str]:
        seen["loop"] = asyncio.get_running_loop()
        seen["locked"] = gate.locked
        return {"status": "ok"}

    with job_loop() as job:
        assert job.run(gate.run(body, reimports=False)) == {"status": "ok"}
    assert seen == {"loop": serving, "locked": True}
    assert not gate.locked


def test_the_bodys_failure_raises_in_the_job(serving) -> None:
    gate = ReloadGate()

    async def body() -> None:
        raise RuntimeError("reload blew up")

    with job_loop() as job, pytest.raises(RuntimeError, match=r"^reload blew up$"):
        job.run(gate.run(body, reimports=True))
    assert not gate.locked
    assert not fork_gate.blocked


def test_a_cancelled_caller_cancels_the_body_on_the_serving_loop(serving) -> None:
    gate = ReloadGate()
    entered = threading.Event()
    cancelled = threading.Event()

    async def body() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def caller() -> None:
        task = asyncio.ensure_future(gate.run(body, reimports=False))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    with job_loop() as job:
        job.run(caller())
    assert cancelled.wait(5)


def test_a_cancelled_caller_holds_the_lock_until_the_body_has_unwound_on_the_serving_loop(serving) -> None:
    gate = ReloadGate()
    entered = threading.Event()
    finish_cleanup = threading.Event()
    second_ran = threading.Event()
    order: list[str] = []

    async def first() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.to_thread(finish_cleanup.wait, 5)
            order.append("first unwound")

    async def second() -> None:
        order.append("second ran")
        second_ran.set()

    async def caller() -> None:
        unwinding = asyncio.ensure_future(gate.run(first, reimports=True))
        await asyncio.to_thread(entered.wait, 5)
        unwinding.cancel()
        await asyncio.sleep(0)
        unwinding.cancel()
        waiting = asyncio.ensure_future(gate.run(second, reimports=False))
        assert not await asyncio.to_thread(second_ran.wait, 0.5)
        assert not unwinding.done()
        assert gate.locked
        assert fork_gate.blocked
        finish_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await unwinding
        await waiting

    with job_loop() as job:
        job.run(caller())
    assert order == ["first unwound", "second ran"]
    assert not gate.locked
    assert not fork_gate.blocked


def test_a_caller_cancelled_before_the_body_starts_returns_once_the_body_is_cancelled(
    serving, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = ReloadGate()
    serving_held = threading.Event()
    release_serving = threading.Event()
    handed = threading.Event()
    ran: list[bool] = []

    class _HandedTask(state._ServingLoopTask):
        def __init__(self, *args: Any) -> None:
            super().__init__(*args)
            handed.set()

    monkeypatch.setattr(state, "_ServingLoopTask", _HandedTask)

    def hold_serving() -> None:
        serving_held.set()
        release_serving.wait(5)

    async def body() -> None:
        ran.append(True)

    async def caller() -> None:
        serving.call_soon_threadsafe(hold_serving)
        await asyncio.to_thread(serving_held.wait, 5)
        task = asyncio.ensure_future(gate.run(body, reimports=False))
        # The body is handed to the serving loop, which is held, so it has not started.
        assert await asyncio.to_thread(handed.wait, 5)
        task.cancel()
        # The caller's cancel reaches its wait for the body's end, which the held loop keeps pending.
        for _ in range(3):
            await asyncio.sleep(0)
        assert not task.done()
        release_serving.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    with job_loop() as job:
        job.run(caller())
    assert ran == []
    assert not gate.locked


def test_a_body_that_fails_while_unwinding_from_a_cancel_raises_its_failure_in_the_job(serving) -> None:
    gate = ReloadGate()
    entered = threading.Event()

    async def body() -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            raise RuntimeError("cleanup blew up")

    async def caller() -> None:
        task = asyncio.ensure_future(gate.run(body, reimports=False))
        await asyncio.to_thread(entered.wait, 5)
        task.cancel()
        with pytest.raises(RuntimeError, match=r"^cleanup blew up$"):
            await task

    with job_loop() as job:
        job.run(caller())
    assert not gate.locked


def test_the_body_runs_in_the_callers_context(serving) -> None:
    gate = ReloadGate()
    seen: list[str] = []

    async def body() -> None:
        seen.append(_REQUEST_SCOPE.get())

    async def request() -> None:
        _REQUEST_SCOPE.set("request-7")
        await gate.run(body, reimports=False)

    with job_loop() as job:
        job.run(request())
    assert seen == ["request-7"]


def test_a_reimporting_body_a_job_runs_passes_that_jobs_span_and_counts_as_the_rebuilds_own_work(serving) -> None:
    gate = ReloadGate()
    seen: dict[str, Any] = {}

    async def body() -> None:
        seen["loop"] = asyncio.get_running_loop()
        seen["blocked"] = fork_gate.blocked
        with fork_gate._cond:
            seen["own_work"] = fork_gate._runs_own_work()

    async def job_with_a_span() -> None:
        async with fork_gate.job_span_async(timeout=5.0):
            await gate.run(body, reimports=True)

    with job_loop() as job:
        job.run(job_with_a_span(), timeout=10)
    assert seen == {"loop": serving, "blocked": True, "own_work": True}
    assert not fork_gate.blocked


def test_a_serving_loop_bound_but_not_running_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    stopped = asyncio.new_event_loop()
    monkeypatch.setattr(instance, "_app", SimpleNamespace(serving_loop=lambda: stopped))
    gate = ReloadGate()
    ran: list[bool] = []

    async def body() -> None:
        ran.append(True)

    try:
        with job_loop() as job, pytest.raises(RuntimeError) as raised:
            job.run(gate.run(body, reimports=False))
    finally:
        stopped.close()
    assert str(raised.value) == "the serving loop is bound but not running, so the work handed to it cannot run"
    assert ran == []
    assert not gate.locked
