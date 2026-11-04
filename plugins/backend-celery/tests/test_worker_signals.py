"""The worker's signal receivers: child init names the macOS fork hazard, and the process that ran
the jobs (a prefork child at its exit, or the worker of an in-process pool as it stops) closes every
loop of the jobs' own, with the kit resources bound to it, lets the host's loop a green pool's jobs
shared go, then flushes spans, once. The worker's build connects them for its app, once however
often it connects them."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import pytest
from celery import signals
from tai42_kit.backend import private_loop

from tai42_backend_celery.core import job_slots, worker_signals
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.tasks import AsyncTask, callback_task, tool_execution


@pytest.fixture(autouse=True)
def _connected() -> None:
    """The receivers the worker's build connects for its app; the suite restores celery's receivers after."""
    worker_signals.connect(celery_app)


def _receiver_counts() -> list[int]:
    return [
        len(signal.receivers or [])
        for signal in (signals.worker_process_init, signals.worker_process_shutdown, signals.worker_shutdown)
    ]


def test_connecting_again_for_the_same_app_adds_no_receiver(stub_app) -> None:
    counts = _receiver_counts()

    worker_signals.connect(celery_app)

    assert _receiver_counts() == counts
    signals.worker_process_shutdown.send(sender=None, pid=1, exitcode=0)
    stub_app.monitoring.active.writer.flush.assert_called_once_with()


def test_worker_process_init_warns_nothing_off_macos(stub_app, monkeypatch, caplog) -> None:
    monkeypatch.setattr(worker_signals.sys, "platform", "linux")
    with caplog.at_level(logging.INFO):
        worker_signals.on_worker_process_init(sender="worker-1")
    assert not any("macOS" in r.message for r in caplog.records)


def test_worker_process_init_names_the_darwin_hazard(stub_app, monkeypatch, caplog) -> None:
    monkeypatch.setattr(worker_signals.sys, "platform", "darwin")
    with caplog.at_level(logging.WARNING):
        worker_signals.on_worker_process_init(sender="worker-1")
    assert any("macOS" in r.message and r.levelno == logging.WARNING for r in caplog.records)


def test_worker_process_shutdown_closes_loops_and_flushes(stub_app, loop_releases) -> None:
    taken = []
    for task in (tool_execution, callback_task):
        loops = task.loops
        taken.append((task, loops, loops.run(_running_loop())))
    signals.worker_process_shutdown.send(sender=None, pid=1, exitcode=0)
    stub_app.monitoring.active.writer.flush.assert_called_once_with()
    assert [(loops.count, loop.is_closed()) for _, loops, loop in taken] == [(0, True), (0, True)]
    assert loop_releases.calls == 2
    assert all(task.loops is not loops for task, loops, _ in taken), "a task kept its closed loops"


def _buffer_spans_and_run_a_job_leaving_a_background_task(stub_app, monkeypatch) -> tuple[list[str], list[str]]:
    """Buffer spans until a flush exports them; a loop's kit release and the job's cancelled task each write one.

    Returns the buffered and the exported spans.
    """
    buffered: list[str] = []
    exported: list[str] = []

    def _flush() -> None:
        exported.extend(buffered)
        buffered.clear()

    stub_app.monitoring.active.writer.flush.side_effect = _flush

    async def _release() -> None:
        buffered.append("loop-bound kit resources released")

    monkeypatch.setattr(private_loop, "release_loop_bound_resources", _release)

    async def _job() -> None:
        async def _background() -> None:
            try:
                await asyncio.sleep(3600)
            finally:
                buffered.append("background task cancelled")

        asyncio.get_running_loop().create_task(_background())
        await asyncio.sleep(0)

    tool_execution.push_request(id="span-at-close")
    try:
        tool_execution.run_async(_job())
    finally:
        tool_execution.pop_request()
    return buffered, exported


def test_a_span_written_while_a_task_s_loop_closes_is_flushed(stub_app, monkeypatch) -> None:
    buffered, exported = _buffer_spans_and_run_a_job_leaving_a_background_task(stub_app, monkeypatch)

    worker_signals.shut_down_jobs(celery_app)

    assert exported == ["loop-bound kit resources released", "background task cancelled"]
    assert buffered == []


def test_worker_shutdown_outside_a_watched_worker_shuts_down_nothing(stub_app, loop_releases) -> None:
    signals.worker_shutdown.send(sender=None)

    stub_app.monitoring.active.writer.flush.assert_not_called()
    assert loop_releases.calls == 0


@pytest.mark.parametrize("pool_name", ["solo", "threads"])
def test_the_worker_s_shutdown_shuts_down_a_pool_s_jobs_where_they_run_in_the_worker(
    stub_app, loop_releases, pool_name: str
) -> None:
    job_slots.watch(
        celery_app,
        pool_name=pool_name,
        in_process=True,
        green=False,
        budget=300.0,
        ticks_in_job=False,
        tick=lambda: None,
    )
    loops = [tool_execution.loops.run(_running_loop()), callback_task.loops.run(_running_loop())]

    signals.worker_shutdown.send(sender=None)

    stub_app.monitoring.active.writer.flush.assert_called_once_with()
    assert [loop.is_closed() for loop in loops] == [True, True]
    assert loop_releases.calls == len(set(loops))


@pytest.mark.parametrize("pool_name", ["gevent", "eventlet"])
def test_the_worker_s_shutdown_on_a_green_pool_lets_the_host_s_loop_go_and_flushes(
    stub_app, loop_releases, host_loop: asyncio.AbstractEventLoop, pool_name: str
) -> None:
    """The host's loop and the kit resources kept for it are the host's: its stop releases them, not the worker's."""
    job_slots.watch(
        celery_app,
        pool_name=pool_name,
        in_process=True,
        green=True,
        budget=300.0,
        ticks_in_job=False,
        tick=lambda: None,
    )
    AsyncTask.share_loop(celery_app, host_loop)
    assert [task.loops.run(_running_loop()) for task in (tool_execution, callback_task)] == [host_loop, host_loop]

    signals.worker_shutdown.send(sender=None)

    stub_app.monitoring.active.writer.flush.assert_called_once_with()
    with pytest.raises(RuntimeError, match=r"^the jobs' shared event loop was let go; it runs no further job$"):
        tool_execution.loops.run(_running_loop())
    assert not host_loop.is_closed()
    assert loop_releases.calls == 0


def test_the_worker_s_shutdown_shuts_down_nothing_where_the_pool_s_jobs_run_in_children(
    stub_app, loop_releases
) -> None:
    job_slots.watch(
        celery_app,
        pool_name="prefork",
        in_process=False,
        green=False,
        budget=300.0,
        ticks_in_job=False,
        tick=lambda: None,
    )
    loop = tool_execution.loops.run(_running_loop())

    signals.worker_shutdown.send(sender=None)

    stub_app.monitoring.active.writer.flush.assert_not_called()
    assert not loop.is_closed()
    assert loop_releases.calls == 0


async def _running_loop() -> asyncio.AbstractEventLoop:
    return asyncio.get_running_loop()


def test_worker_process_shutdown_closes_every_task_s_loops_and_raises_a_failed_flush(stub_app) -> None:
    stub_app.monitoring.active.writer.flush.side_effect = RuntimeError("exporter gone")
    taken = []
    for task in (tool_execution, callback_task):
        taken.append(task.loops.run(_running_loop()))

    with pytest.raises(ExceptionGroup, match="shutting down the jobs' work failed") as raised:
        worker_signals.shut_down_jobs(celery_app)

    assert [str(failure) for failure in raised.value.exceptions] == ["exporter gone"]
    assert [loop.is_closed() for loop in taken] == [True, True]


def test_worker_process_shutdown_raises_each_failed_close_and_a_failed_flush_together(stub_app, monkeypatch) -> None:
    stub_app.monitoring.active.writer.flush.side_effect = RuntimeError("exporter gone")

    def _boom() -> None:
        raise RuntimeError("loop wedged")

    with monkeypatch.context() as patched:
        patched.setattr(tool_execution, "close_loop", _boom)
        with pytest.raises(ExceptionGroup) as raised:
            worker_signals.shut_down_jobs(celery_app)

    assert [str(failure) for failure in raised.value.exceptions] == ["loop wedged", "exporter gone"]


def test_worker_process_shutdown_closes_every_task_s_loops_and_raises_each_failed_close(stub_app, monkeypatch) -> None:
    closed: list[str] = []

    def _fails(task) -> Callable[[], None]:
        def _close() -> None:
            closed.append(task.name)
            raise RuntimeError(f"{task.name} loop wedged")

        return _close

    with monkeypatch.context() as patched:
        for task in (tool_execution, callback_task):
            patched.setattr(task, "close_loop", _fails(task))
        with pytest.raises(ExceptionGroup) as raised:
            worker_signals.shut_down_jobs(celery_app)

    assert closed == [tool_execution.name, callback_task.name]
    assert [str(failure) for failure in raised.value.exceptions] == [
        f"{tool_execution.name} loop wedged",
        f"{callback_task.name} loop wedged",
    ]


def test_worker_process_shutdown_closes_the_next_task_s_loops_after_a_failed_close(stub_app, monkeypatch) -> None:
    loop = callback_task.loops.run(_running_loop())

    def _boom() -> None:
        raise RuntimeError("loop wedged")

    with monkeypatch.context() as patched:
        patched.setattr(tool_execution, "close_loop", _boom)
        with pytest.raises(ExceptionGroup, match="shutting down the jobs' work failed"):
            worker_signals.shut_down_jobs(celery_app)

    assert loop.is_closed()
    stub_app.monitoring.active.writer.flush.assert_called_once_with()


def test_a_failed_loop_close_at_worker_child_shutdown_is_logged_with_its_traceback(
    stub_app, monkeypatch, caplog
) -> None:
    def _boom() -> None:
        raise RuntimeError("loop wedged")

    with monkeypatch.context() as patched, caplog.at_level(logging.ERROR):
        patched.setattr(tool_execution, "close_loop", _boom)
        signals.worker_process_shutdown.send(sender=None, pid=1, exitcode=0)

    [logged] = [r for r in caplog.records if r.exc_info and r.exc_info[0] is ExceptionGroup]
    assert "on_worker_process_shutdown" in logged.getMessage()


def test_a_failed_loop_close_at_the_worker_s_shutdown_is_logged_with_its_traceback(
    stub_app, monkeypatch, caplog
) -> None:
    job_slots.watch(
        celery_app,
        pool_name="threads",
        in_process=True,
        green=False,
        budget=300.0,
        ticks_in_job=False,
        tick=lambda: None,
    )

    def _boom() -> None:
        raise RuntimeError("loop wedged")

    with monkeypatch.context() as patched, caplog.at_level(logging.ERROR):
        patched.setattr(tool_execution, "close_loop", _boom)
        signals.worker_shutdown.send(sender=None)

    [logged] = [r for r in caplog.records if r.exc_info and r.exc_info[0] is ExceptionGroup]
    assert "on_worker_shutdown" in logged.getMessage()
    assert [str(failure) for failure in logged.exc_info[1].exceptions] == ["loop wedged"]


@pytest.fixture(autouse=True)
def _reset_task_loops():
    yield
    for task in (tool_execution, callback_task):
        task.close_loop()
    AsyncTask.close_shared_loop(celery_app)
