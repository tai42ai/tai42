"""The work engaged with the reload gate, as a retiring generation sees it.

A request or a tool-run supervisor that drives or waits on the reload gate cannot finish
before the gate is released, so a retire neither counts it against the drain budget nor
severs it. These cases cover the shapes that reach the gate from somewhere other than the
request's or the supervisor's own task: a supervised run whose tool call runs in a child
task, a request that starts waiting after the retire began, two calls sharing one
request record (an SSE session), and a driver acting through a long-lived stream.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from tai42_skeleton.app import epoch_retire as epoch_retire_mod
from tai42_skeleton.app.admission import mark_current_request_drain_exempt
from tai42_skeleton.app.epoch import build_and_swap_epoch, current_epoch
from tai42_skeleton.app.reload_gate import reload_gate

from .test_epoch import (  # noqa: F401 - _reset_epoch_state is the autouse fixture these cases need
    _ClosingSupervisor,
    _install_boot,
    _reset_epoch_state,
    _serve,
    _served_request,
)

DRAIN_ERROR = "still in flight after the drain budget"


async def _deferred_tails_finished() -> None:
    async with asyncio.timeout(2.0):
        await asyncio.gather(*epoch_retire_mod._deferred_retire_tasks)


async def test_a_supervised_run_whose_tool_call_runs_in_a_child_task_survives_its_own_retire() -> None:
    """An agent run as a background run calls its tools in child tasks (its tool node gathers
    them); the call that drives a reload is still the run's work, so the retire it drives
    leaves the run running and it records its real outcome."""
    from tai42_skeleton.operations.tool_runs import supervisor as supervisor_mod

    _install_boot("boot-app")
    boot = current_epoch()
    outcome: list[str] = []

    async def _tool_call() -> None:
        async with reload_gate.driving():
            await build_and_swap_epoch(
                {}, rebuild=lambda: None, build_serving_app=_serve("new-app"), drain_deadline=0.5
            )
        await asyncio.sleep(0)  # the reload's fleet wait runs after the swap
        outcome.append("tool call completed")

    async def _agent_run() -> None:
        await asyncio.gather(_tool_call())
        outcome.append("run completed")

    task = asyncio.create_task(_agent_run())
    supervisor_mod._enroll_supervisor(task)
    task.add_done_callback(supervisor_mod._discard_supervisor)
    try:
        await task
    except asyncio.CancelledError as exc:
        outcome.append(f"cancelled: {exc}")
    assert outcome == ["tool call completed", "run completed"]
    await _deferred_tails_finished()
    assert not boot.driving_tasks


async def test_a_task_outliving_its_supervisor_drives_nothing() -> None:
    """A task that copied a supervised run's context and outlived the run acts for no run."""
    from tai42_skeleton.operations.tool_runs import supervisor as supervisor_mod

    _install_boot("boot-app")
    boot = current_epoch()
    spawned: list[asyncio.Task[None]] = []
    go = asyncio.Event()

    async def _later() -> None:
        await go.wait()
        async with reload_gate.driving():
            pass

    async def _run() -> None:
        spawned.append(asyncio.create_task(_later()))

    task = asyncio.create_task(_run())
    supervisor_mod._enroll_supervisor(task)
    task.add_done_callback(supervisor_mod._discard_supervisor)
    await task
    assert supervisor_mod.running_supervisor() is None
    go.set()
    await spawned[0]
    assert boot.driving_tasks == set()
    assert not boot.waiting_tasks


async def test_a_request_waiting_on_the_gate_leaves_the_in_flight_count_while_it_waits() -> None:
    """While a request waits on the gate it cannot move, so it is not counted; once it holds
    the gate without driving it is ordinary in-flight work again."""
    _install_boot("boot-app")
    boot = current_epoch()
    acquired = asyncio.Event()
    release = asyncio.Event()

    async def _lookup() -> None:
        async with reload_gate.synchronized():
            acquired.set()
            await release.wait()

    async with reload_gate.synchronized():
        request = _served_request(boot, _lookup)
        while not boot.waiting:
            await asyncio.sleep(0)
        assert boot.in_flight == 0
    await acquired.wait()
    assert boot.in_flight == 1
    assert not boot.waiting
    release.set()
    await request
    assert boot.in_flight == 0


async def test_a_request_reaching_the_gate_during_a_synchronous_drain_is_not_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A bus-delivered reload (no driver) drains synchronously; a request of the retiring
    generation that starts waiting on the gate meanwhile is not waited for and not reported:
    the retire defers, and the request is never severed while it waits."""
    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    go = asyncio.Event()
    budget = 0.5

    async def _lookup() -> None:
        await go.wait()
        async with reload_gate.synchronized():
            log.append("waited out")
        log.append("answered")

    request = _served_request(boot, _lookup)
    await asyncio.sleep(0)
    loop = asyncio.get_running_loop()
    with caplog.at_level(logging.ERROR):
        async with reload_gate.synchronized():  # the bus-delivered reload holds the gate
            loop.call_later(0.01, go.set)
            start = loop.time()
            await build_and_swap_epoch(
                {}, rebuild=lambda: None, build_serving_app=_serve("new-app"), drain_deadline=budget
            )
            took = loop.time() - start
            assert log == [], "the retire closed under a request waiting on the gate"
        await request
        await _deferred_tails_finished()
    assert took < budget / 2, f"the synchronous drain waited {took:.3f}s on a request blocked on the gate"
    assert log == ["waited out", "answered", "closed"]
    assert DRAIN_ERROR not in caplog.text


async def test_a_request_waiting_on_the_gate_after_the_tail_turned_over_is_never_severed() -> None:
    """The retire's tail has let the gate turn over and waits for its driver; another reload
    then holds the gate and a request of the retiring generation starts waiting on it. The
    tail closes the transports only after that request has acquired the gate and answered."""
    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    driver_answers = asyncio.Event()
    other_holds = asyncio.Event()
    release_other = asyncio.Event()

    async def _driver() -> None:
        async with reload_gate.driving():
            await build_and_swap_epoch(
                {}, rebuild=lambda: None, build_serving_app=_serve("new-app"), drain_deadline=0.05
            )
        await driver_answers.wait()
        log.append("driver answered")

    async def _other_reload() -> None:
        async with reload_gate.synchronized():
            other_holds.set()
            await release_other.wait()

    async def _late_lookup() -> None:
        await other_holds.wait()
        async with reload_gate.synchronized():
            log.append("late waiter acquired")
        log.append("late waiter answered")

    driver = _served_request(boot, _driver)
    await asyncio.sleep(0.2)  # swapped; the tail turned the gate over, drained, and waits for the driver
    late = _served_request(boot, _late_lookup)
    other = asyncio.create_task(_other_reload())
    await other_holds.wait()
    while not boot.waiting:
        await asyncio.sleep(0)
    driver_answers.set()
    await driver
    await asyncio.sleep(0.1)
    assert log == ["driver answered"], f"the tail closed under a request waiting on the gate: {log}"
    release_other.set()
    await other
    await late
    await _deferred_tails_finished()
    assert log == ["driver answered", "late waiter acquired", "late waiter answered", "closed"]


async def test_two_calls_sharing_one_request_record_wait_on_the_gate_independently() -> None:
    """Two calls acting for one request (an SSE session runs every call under its stream's
    record) wait on the gate together; one giving up leaves the other recorded as waiting."""
    _install_boot("boot-app")
    boot = current_epoch()
    waiters: list[asyncio.Task[None]] = []
    finish = asyncio.Event()

    async def _waiter() -> None:
        async with reload_gate.synchronized():
            pass

    async def _session() -> None:
        waiters.extend(asyncio.create_task(_waiter()) for _ in range(2))
        await finish.wait()

    async with reload_gate.synchronized():
        request = _served_request(boot, _session)
        while len(waiters) < 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert boot.waiting
        waiters[0].cancel()
        await asyncio.gather(waiters[0], return_exceptions=True)
        assert boot.waiting, "one call giving up dropped the other's wait"
    await waiters[1]
    assert not boot.waiting
    finish.set()
    await request
    assert boot.in_flight == 0


async def test_a_driver_acting_through_a_long_lived_stream_is_not_awaited_by_the_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A reload driven by a call of an SSE session (every call answers on the session's
    stream, which exempted itself from the drain) defers the retire, so the reload answers
    at once; the stream ends only when its client disconnects and the transport close does
    not sever it, so the close does not wait for it."""
    _install_boot("boot-app")
    boot = current_epoch()
    log: list[str] = []
    boot.supervisor = _ClosingSupervisor(log)  # type: ignore[assignment]
    disconnect = asyncio.Event()
    loop = asyncio.get_running_loop()
    took: list[float] = []

    async def _session_stream() -> None:
        mark_current_request_drain_exempt()
        async with reload_gate.driving():
            start = loop.time()
            await build_and_swap_epoch(
                {}, rebuild=lambda: None, build_serving_app=_serve("new-app"), drain_deadline=0.5
            )
            took.append(loop.time() - start)
        log.append("driver answered")
        await disconnect.wait()

    with caplog.at_level(logging.ERROR):
        stream = _served_request(boot, _session_stream)
        try:
            await _deferred_tails_finished_after_the_swap(took)
            assert log == ["driver answered", "closed"]
        finally:
            disconnect.set()
            await stream
    assert took[0] < 0.25, "the reload drained synchronously inside its own driver"
    assert DRAIN_ERROR not in caplog.text


async def _deferred_tails_finished_after_the_swap(took: list[float]) -> None:
    async with asyncio.timeout(2.0):
        while not took:
            await asyncio.sleep(0)
    await _deferred_tails_finished()
