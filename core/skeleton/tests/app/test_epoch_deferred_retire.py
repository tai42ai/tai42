"""The deferred retire tail of an engaged generation: what it drains once the recorded waits end, and how it fails.

An engaged generation's retire hands its drain and its close to a background tail. Work that
waited on a recorded lock is left alone while it waits; once the wait ends it is ordinary work
and the tail drains it like the synchronous retire does, the tool-run supervisors included. A
retire that has seen the generation engaged hands it to the tail even when that work stops
waiting before the retire returns, so the work a synchronous drain left out is still drained. A
failure of the tail itself is logged at ERROR with its traceback.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from tai42_contract.app import tai42_app
from tests._fakes.rebuild import no_rebuild

from tai42_skeleton.app import epoch_retire as epoch_retire_mod
from tai42_skeleton.app.epoch import Epoch, build_and_swap_epoch, current_epoch
from tai42_skeleton.app.reload_gate import reload_gate
from tai42_skeleton.config.deployment_env import DeploymentOwnership

from ._deferred_retire_tail import DEFERRED_TAIL_BOUND_S
from .test_epoch import (  # noqa: F401 - _reset_epoch_state is the autouse fixture these cases need
    _ClosingSupervisor,
    _install_boot,
    _reset_epoch_state,
    _serve,
    _served_request,
)


async def _bus_delivered_reload(drain_deadline: float) -> None:
    """A reload delivered over the bus: no request of its own holds the gate while it retires."""
    async with reload_gate.synchronized():
        await build_and_swap_epoch(
            {},
            ownership=DeploymentOwnership(),
            rebuild=no_rebuild,
            build_serving_app=_serve("new-app"),
            drain_deadline=drain_deadline,
        )


def _supervised(body: Callable[[], Coroutine[Any, Any, None]]) -> asyncio.Task[None]:
    """Run ``body`` as a background run enrolled with the live generation, as the submit door enrolls one."""
    from tai42_skeleton.operations.tool_runs import supervisor as supervisor_mod

    run = asyncio.create_task(body())
    supervisor_mod._enroll_supervisor(run)
    run.add_done_callback(supervisor_mod._discard_supervisor)
    return run


async def _until_cancelled(log: list[str], name: str) -> None:
    """A run's remaining work, which never finishes on its own: logs the cancel that ends it."""
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError as exc:
        log.append(f"{name} cancelled: {exc}")
        raise


_RETIRE_REASON = "the serving epoch was retired by a config apply before the tool-run completed"


async def test_a_supervised_run_leaving_its_recorded_wait_is_drained_by_the_deferred_tail() -> None:
    """A background run of the retiring generation waits on fleet work while the reload retires it,
    so the retire neither drains nor cancels it. Once the wait ends, the run is ordinary work of
    that generation: the deferred tail drains it within the budget, cancelling it with the retire's
    reason, before it closes the transports."""
    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    job_done = asyncio.Event()

    async def _supervised_run() -> None:
        async with tai42_app.lifecycle.waiting_on_fleet_work():
            await job_done.wait()
        log.append("left the wait")
        await _until_cancelled(log, "run")

    run = _supervised(_supervised_run)
    while not boot.waiting_tasks:
        await asyncio.sleep(0)
    await _bus_delivered_reload(drain_deadline=0.05)
    [tail] = epoch_retire_mod.deferred_retires()
    for _ in range(20):
        await asyncio.sleep(0)
    assert log == [], "the retire drained or closed under a run in a recorded wait"
    job_done.set()
    async with asyncio.timeout(DEFERRED_TAIL_BOUND_S):
        await tail
    assert run.done(), "the deferred tail closed the generation and left its run running"
    assert log == [
        "left the wait",
        f"run cancelled: {_RETIRE_REASON}",
        "closed",
    ]


async def test_a_failing_deferred_tail_is_logged_at_error_with_its_traceback(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The deferred tail runs in the background with nobody awaiting it, so its own failure is
    logged at ERROR, naming the task, with the exception's traceback."""
    _install_boot("boot-app")
    boot = current_epoch()
    boot.supervisor = _ClosingSupervisor([])  # type: ignore[assignment]
    job_done = asyncio.Event()

    def _failing_certification(old: Epoch, retired: int) -> None:
        raise RuntimeError("certification exploded")

    monkeypatch.setattr(epoch_retire_mod, "arm_retired_generation_certification", _failing_certification)

    async def _waiter() -> None:
        async with tai42_app.lifecycle.waiting_on_fleet_work():
            await job_done.wait()

    request = _served_request(boot, _waiter)
    while not boot.waiting:
        await asyncio.sleep(0)
    with caplog.at_level(logging.ERROR, logger=epoch_retire_mod.__name__):
        await _bus_delivered_reload(drain_deadline=0.05)
        [tail] = epoch_retire_mod.deferred_retires()
        job_done.set()
        async with asyncio.timeout(2.0):
            await request
            with pytest.raises(RuntimeError, match="certification exploded"):
                await tail
        await asyncio.sleep(0)  # the done-callbacks run on the loop's next turn
    [record] = [r for r in caplog.records if r.name == epoch_retire_mod.__name__ and r.levelno == logging.ERROR]
    assert f"tai-epoch-{boot.number}-deferred-retire" in record.getMessage()
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)


async def test_a_wait_that_ends_during_the_run_drain_leaves_the_retire_to_the_tail() -> None:
    """The generation is engaged when the retire starts: a background run waits on fleet work, so
    the synchronous drain of the admitted work is skipped and the run is kept out of the run drain.
    That wait ends while the run drain is cancelling another run. The retire still hands the drain
    and the close to the tail: the admitted request in flight is drained within the budget and the
    run that waited is drained, before the transports close."""
    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    job_done = asyncio.Event()

    async def _waiting_run() -> None:
        async with tai42_app.lifecycle.waiting_on_fleet_work():
            await job_done.wait()
        log.append("left the wait")
        await _until_cancelled(log, "waiting run")

    async def _ordinary_run() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            job_done.set()  # the fleet work the other run waits on ends during the run drain
            while boot.waiting_tasks:
                await asyncio.sleep(0)
            log.append("ordinary run cancelled")
            raise

    request_released = asyncio.Event()

    async def _request() -> None:
        await request_released.wait()  # still in flight once the retire has handed over to the tail
        log.append("request answered")

    waiting_run = _supervised(_waiting_run)
    ordinary_run = _supervised(_ordinary_run)
    request = _served_request(boot, _request)
    while not boot.waiting_tasks or not boot.in_flight:
        await asyncio.sleep(0)
    await _bus_delivered_reload(drain_deadline=1.0)
    assert "closed" not in log, f"the retire closed under the generation's undrained work: {log}"
    [tail] = epoch_retire_mod.deferred_retires()
    request_released.set()
    async with asyncio.timeout(DEFERRED_TAIL_BOUND_S):
        await tail
    await request
    assert waiting_run.done(), "the retire closed the generation and left the run that waited running"
    assert ordinary_run.done()
    assert log == [
        "left the wait",
        "ordinary run cancelled",
        "request answered",
        f"waiting run cancelled: {_RETIRE_REASON}",
        "closed",
    ]


async def test_a_request_that_starts_waiting_during_the_run_drain_leaves_the_retire_to_the_tail() -> None:
    """The generation is not engaged when the retire starts. An admitted request outlives the
    drain budget of the synchronous admitted drain, then starts a recorded wait on fleet work while
    the run drain cancels a background run, and is still waiting once that drain has ended. The
    retire hands the generation to the tail instead of closing it under the waiting request: the
    tail closes the transports only after the request has left its wait and answered."""
    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    run_drain_started = asyncio.Event()
    job_done = asyncio.Event()

    async def _ordinary_run() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run_drain_started.set()  # the request starts its wait during the run drain
            while not boot.waiting:
                await asyncio.sleep(0)
            log.append("ordinary run cancelled")
            raise

    async def _request() -> None:
        await run_drain_started.wait()
        async with tai42_app.lifecycle.waiting_on_fleet_work():
            await job_done.wait()
        log.append("request answered")

    ordinary_run = _supervised(_ordinary_run)
    request = _served_request(boot, _request)
    while not boot.in_flight:
        await asyncio.sleep(0)
    await _bus_delivered_reload(drain_deadline=0.05)
    assert boot.waiting, "the request left its recorded wait before the retire returned"
    assert "closed" not in log, f"the retire closed the generation under a request in a recorded wait: {log}"
    [tail] = epoch_retire_mod.deferred_retires()
    job_done.set()
    async with asyncio.timeout(DEFERRED_TAIL_BOUND_S):
        await tail
        await request
    assert ordinary_run.done()
    assert log == [
        "ordinary run cancelled",
        "request answered",
        "closed",
    ]


async def test_a_run_kept_out_of_the_run_drain_while_waiting_is_drained_before_the_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generation is not engaged when the retire starts. While the synchronous drain waits on
    an admitted request, the test records a wait of a background run of the generation on the
    epoch's wait state directly (``Epoch.begin_task_wait``), so the run drain keeps that run out.
    The real recorders cannot start such a wait after the swap: they record a background run only
    while its generation is live (see the ``epoch_retire`` module docstring). The wait ends while
    the run drain cancels another run. The run that was kept out is still drained before the
    transports close, as is the admitted request."""
    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    waiting_run = _supervised(lambda: _until_cancelled(log, "waiting run"))

    async def _ordinary_run() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            boot.end_task_wait(waiting_run)  # the kept-out run's wait ends during the run drain
            log.append("ordinary run cancelled")
            raise

    request_released = asyncio.Event()

    async def _request() -> None:
        await request_released.wait()  # still in flight once the retire has handed over to the tail
        log.append("request answered")

    ordinary_run = _supervised(_ordinary_run)
    drain = boot._drain_in_flight

    wait_started = False

    async def _drain_while_a_run_starts_waiting(deadline: float, **kwargs: bool) -> bool:
        nonlocal wait_started
        if not wait_started:
            wait_started = True
            asyncio.get_running_loop().call_soon(boot.begin_task_wait, waiting_run)
        return await drain(deadline, **kwargs)

    monkeypatch.setattr(boot, "_drain_in_flight", _drain_while_a_run_starts_waiting)
    request = _served_request(boot, _request)
    while not boot.in_flight:
        await asyncio.sleep(0)
    await _bus_delivered_reload(drain_deadline=1.0)
    [tail] = epoch_retire_mod.deferred_retires()
    request_released.set()
    async with asyncio.timeout(DEFERRED_TAIL_BOUND_S):
        await tail
    await request
    assert waiting_run.done(), "the retire closed the generation and left the run that waited running"
    assert ordinary_run.done()
    assert log == [
        "ordinary run cancelled",
        "request answered",
        f"waiting run cancelled: {_RETIRE_REASON}",
        "closed",
    ]
