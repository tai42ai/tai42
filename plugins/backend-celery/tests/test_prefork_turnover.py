"""The Celery-shaped half of the pool turnover: the pool guards, the restart, the
bounded restart propagation with its kill-and-respawn guard, and the turnover
confirmation that RAISES on a pool that did not fully recycle.

Which ops warrant a turnover, how the budget is sized, and the manifest env the
re-forked children inherit are the shared backend base's — covered by the kit
suite, not repeated here. This module drives ``turnover_local_pool`` with a budget
handed in, exactly as the runtime does.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

import tai42_backend_celery.core.prefork as prefork
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.backend import CeleryWorkerRuntime

_HOST = "celery@node-1"

# A budget generous enough that only a deliberately wedged path can exhaust it —
# the base derives the real one from the bus apply window.
_BUDGET = 5.0


class _FakeInspect:
    def __init__(self, stats: Any, control: _FakeControl) -> None:
        self._stats = stats
        self._control = control

    def stats(self) -> Any:
        return self._stats

    def active(self) -> Any:
        return self._control.next_active()


class _FakeControl:
    """Models ``inspect().stats()`` (a scripted sequence of pool snapshots),
    ``inspect().active()`` (a scripted sequence of the children's running jobs, none by
    default) and ``broadcast('pool_restart')`` (an arm reply, an error to raise, or — with
    ``wedge`` set — a propagation that never returns, the billiard deadlock)."""

    def __init__(self, stats_sequence: list[Any], active_sequence: list[Any] | None = None) -> None:
        self._stats_sequence = stats_sequence
        self._last: Any = {}
        self._active_sequence = active_sequence or []
        self._last_active: Any = {}
        self.broadcasts: list[tuple[str, Any]] = []
        self.connections: list[Any] = []
        self.restart_reply: Any = [{_HOST: {"ok": "pool restart"}}]
        self.wedge: threading.Event | None = None
        self.on_restart: Callable[[], None] | None = None
        self.on_inspect: Callable[[], None] | None = None

    def inspect(
        self, destination: Any = None, timeout: Any = None, limit: Any = None, connection: Any = None
    ) -> _FakeInspect:
        # Control I/O rides a dedicated connection, never the shared pool.
        assert connection is not None
        self.connections.append(connection)
        if self.on_inspect is not None:
            self.on_inspect()
        if self._stats_sequence:
            self._last = self._stats_sequence.pop(0)
        return _FakeInspect(self._last, self)

    def next_active(self) -> Any:
        if self._active_sequence:
            self._last_active = self._active_sequence.pop(0)
        return self._last_active

    def broadcast(
        self,
        method: str,
        arguments: Any = None,
        reply: Any = None,
        destination: Any = None,
        connection: Any = None,
        **kwargs: Any,
    ) -> Any:
        assert connection is not None
        self.connections.append(connection)
        self.broadcasts.append((method, destination))
        if self.on_restart is not None:
            self.on_restart()
        if self.wedge is not None:
            self.wedge.wait()  # the propagation never comes back on its own
        if isinstance(self.restart_reply, Exception):
            raise self.restart_reply
        return self.restart_reply


def _pool(pids: list[int], max_conc: int = 1, impl: str = "prefork") -> dict[str, Any]:
    return {_HOST: {"pool": {"processes": pids, "max-concurrency": max_conc, "implementation": impl}}}


@pytest.fixture
def runtime() -> CeleryWorkerRuntime:
    """The worker runtime whose turnover the tests drive, as the launch holds it."""
    return CeleryWorkerRuntime.from_args([])


@pytest.fixture
def turnover(runtime: CeleryWorkerRuntime, monkeypatch: pytest.MonkeyPatch) -> prefork.PoolTurnover:
    """The runtime's turnover of a ready worker with a known node name, with polls made instant."""
    held = runtime._turnover
    held._local_nodename = _HOST
    held._worker_ready.set()
    monkeypatch.setattr(prefork, "_POOL_RECYCLE_POLL_INTERVAL", 0.001)
    return held


def _busy(*pids: int) -> dict[str, Any]:
    """An ``active()`` reply: each pid's child is running one job."""
    return {_HOST: [{"id": f"job-{pid}", "worker_pid": pid} for pid in pids]}


def _install(
    monkeypatch: pytest.MonkeyPatch, stats_sequence: list[Any], active_sequence: list[Any] | None = None
) -> _FakeControl:
    control = _FakeControl(stats_sequence, active_sequence)
    monkeypatch.setattr(celery_app, "control", control)
    return control


def _record_kills(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Capture the signals the kill guard would send; no real process is signalled."""
    kills: list[tuple[int, int]] = []
    monkeypatch.setattr(prefork.os, "kill", lambda pid, sig: kills.append((pid, sig)))
    return kills


@pytest.fixture
def wedged_restart() -> Iterator[threading.Event]:
    """A ``pool_restart`` propagation that never returns — the billiard deadlock.
    Released at teardown so the watchdog thread it strands can finish."""
    release = threading.Event()
    yield release
    release.set()


# --- guards -------------------------------------------------------------------


def test_non_prefork_pool_is_a_no_op(monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover) -> None:
    control = _install(monkeypatch, [_pool([111], impl="solo")])
    turnover.turnover_local_pool("reload_config", _BUDGET)
    # No restart broadcast for a pool with no forked children to recycle.
    assert control.broadcasts == []


def test_turnover_skipped_before_worker_setup(monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover) -> None:
    # No node name captured yet: the pool has not forked, so nothing to recycle.
    turnover._local_nodename = None
    control = _install(monkeypatch, [_pool([111])])
    turnover.turnover_local_pool("reload_config", _BUDGET)
    assert control.broadcasts == []


def test_turnover_waits_for_worker_ready_then_recycles(
    monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover
) -> None:
    # Setup reached but not yet consuming: the turnover waits, then recycles.
    turnover._worker_ready.clear()

    def _wait(timeout: float | None = None) -> bool:
        turnover._worker_ready.set()  # the worker starts consuming while we wait
        return True

    monkeypatch.setattr(turnover._worker_ready, "wait", _wait)
    control = _install(monkeypatch, [_pool([111]), _pool([222])])
    turnover.turnover_local_pool("reload_config", _BUDGET)
    assert control.broadcasts == [("pool_restart", [_HOST])]


def test_turnover_raises_if_worker_never_starts_consuming(
    monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover
) -> None:
    # Worker never begins consuming: a possibly-stale child cannot be recycled,
    # so the op fails loudly.
    turnover._worker_ready.clear()
    monkeypatch.setattr(turnover._worker_ready, "wait", lambda timeout=None: False)
    control = _install(monkeypatch, [_pool([111])])
    with pytest.raises(RuntimeError, match="did not start consuming control commands"):
        turnover.turnover_local_pool("reload_config", 0.5)
    assert control.broadcasts == []


def test_worker_that_does_not_answer_stats_raises(
    monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover
) -> None:
    _install(monkeypatch, [{}])
    with pytest.raises(RuntimeError, match="did not answer stats"):
        turnover.turnover_local_pool("reload_config", _BUDGET)


# --- restart + confirm --------------------------------------------------------


def test_full_turnover_restarts_and_confirms(monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover) -> None:
    # Pre-state has child 111; after the restart the pool comes back as child 222.
    control = _install(monkeypatch, [_pool([111]), _pool([222])])
    turnover.turnover_local_pool("reload_config", _BUDGET)
    # The local pool was armed for restart.
    assert control.broadcasts == [("pool_restart", [_HOST])]


def test_the_whole_turnover_is_spent_inside_the_caller_s_budget(
    monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover
) -> None:
    # The budget is the caller's window, not a local constant: a worker that never
    # starts consuming must give up inside it rather than outlive the op's apply.
    turnover._worker_ready.clear()
    _install(monkeypatch, [_pool([111])])
    started = time.monotonic()
    with pytest.raises(RuntimeError, match=r"within 0\.05s"):
        turnover.turnover_local_pool("reload_config", 0.05)
    assert time.monotonic() - started < 2.0


def test_old_child_still_present_raises(monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover) -> None:
    # The pre-restart child (111) never leaves and runs no job — an idle stale child
    # would take new work on the old registry, so the turnover must fail loudly.
    control = _install(monkeypatch, [_pool([111]), _pool([111, 222])])
    monkeypatch.setattr(prefork, "_POOL_RECYCLE_POLL_INTERVAL", 0.0)
    with pytest.raises(RuntimeError, match="idle pre-restart children still present: \\[111\\]"):
        turnover._confirm_turnover(_HOST, ({111}, 1), timeout=0.05)
    assert control.broadcasts == []  # confirm-only helper does not restart


def test_a_pre_restart_child_busy_with_a_job_is_not_waited_for(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    turnover: prefork.PoolTurnover,
) -> None:
    # The pre-restart child (111) is running a job — possibly the very job that published
    # the op this turnover applies. It checks the restart before taking its next job, so it
    # finishes this one and exits after it: the pool at size with no idle survivor is turned
    # over on the first poll.
    control = _install(monkeypatch, [_pool([111])], [_busy(111)])
    monkeypatch.setattr(prefork, "_POOL_RECYCLE_POLL_INTERVAL", 0.0)
    with caplog.at_level(logging.INFO, logger=prefork.logger.name):
        turnover._confirm_turnover(_HOST, ({111}, 1), timeout=5.0)
    assert len(control.connections) == 1
    assert any("111" in r.getMessage() and "exit after" in r.getMessage() for r in caplog.records)


def test_a_busy_pre_restart_child_still_waits_for_the_pool_to_refill(
    monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover
) -> None:
    # The busy child is exempt from "gone", never from the pool's size: a two-child pool
    # holding only the busy pre-restart child is confirmed once the second child is forked.
    control = _install(monkeypatch, [_pool([111], max_conc=2), _pool([111, 222], max_conc=2)], [_busy(111)])
    turnover._confirm_turnover(_HOST, ({111, 112}, 2), timeout=5.0)
    assert len(control.connections) == 2


def test_pool_came_back_short_raises(monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover) -> None:
    # Old child gone but the pool never refills to its configured size.
    _install(monkeypatch, [_pool([], max_conc=2)])
    monkeypatch.setattr(prefork, "_POOL_RECYCLE_POLL_INTERVAL", 0.0)
    with pytest.raises(RuntimeError, match="pool came back short"):
        turnover._confirm_turnover(_HOST, ({111}, 2), timeout=0.05)


def test_pool_restart_arm_failure_raises(monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover) -> None:
    # The arm runs on the watchdog thread; its failure must surface on the caller's.
    control = _install(monkeypatch, [_pool([111])])
    control.restart_reply = [{_HOST: {"error": "no pool"}}]
    with pytest.raises(RuntimeError, match="pool restart request failed"):
        turnover.turnover_local_pool("reload_config", _BUDGET)


def test_normal_turnover_never_kills_pool_children(
    monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover
) -> None:
    # A restart that propagates is left alone: the guard is the wedge path only.
    control = _install(monkeypatch, [_pool([111]), _pool([222])])
    kills = _record_kills(monkeypatch)
    turnover.turnover_local_pool("reload_config", _BUDGET)
    assert control.broadcasts == [("pool_restart", [_HOST])]
    assert kills == []


# --- wedged restart: the bounded kill-and-respawn guard -----------------------


def test_wedged_restart_is_bounded_then_killed_and_confirmed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    wedged_restart: threading.Event,
    turnover: prefork.PoolTurnover,
) -> None:
    # The propagation never returns (billiard deadlock): the guard must fire inside
    # the budget, SIGKILL the pre-restart child, and confirm the re-fork (222).
    control = _install(monkeypatch, [_pool([222])])
    control.wedge = wedged_restart
    kills = _record_kills(monkeypatch)
    started = time.monotonic()
    with caplog.at_level(logging.ERROR, logger=prefork.logger.name):
        turnover._restart_pool_and_confirm(_HOST, ({111}, 1), budget=0.4)
    assert time.monotonic() - started < 0.4  # bounded well inside the caller's budget
    assert control.broadcasts == [("pool_restart", [_HOST])]
    assert kills == [(111, signal.SIGKILL)]
    # The wedge is named, not just reported as a generic timeout.
    assert "_putlock" in caplog.text
    assert "killed wedged pool child 111" in caplog.text


def test_wedged_restart_that_cannot_respawn_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
    wedged_restart: threading.Event,
    turnover: prefork.PoolTurnover,
) -> None:
    # Killed and still not turned over (111 never leaves the pool): the op must
    # fail, naming both the wedge and the failed hard path.
    control = _install(monkeypatch, [_pool([111])])
    control.wedge = wedged_restart
    kills = _record_kills(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        turnover._restart_pool_and_confirm(_HOST, ({111}, 1), budget=0.2)
    assert kills == [(111, signal.SIGKILL)]
    assert "wedged inside billiard (_putlock)" in str(excinfo.value)
    assert "idle pre-restart children still present: [111]" in str(excinfo.value)


def test_confirm_failure_after_a_healthy_restart_is_not_blamed_on_a_wedge(
    monkeypatch: pytest.MonkeyPatch, turnover: prefork.PoolTurnover
) -> None:
    # The restart propagated; only the re-fork failed. The op still fails loudly,
    # but reports what happened rather than a wedge, and nothing is killed.
    control = _install(monkeypatch, [_pool([111])])
    kills = _record_kills(monkeypatch)
    with pytest.raises(RuntimeError) as excinfo:
        turnover._restart_pool_and_confirm(_HOST, ({111}, 1), budget=0.1)
    assert control.broadcasts == [("pool_restart", [_HOST])]
    assert kills == []
    assert "wedged" not in str(excinfo.value)
    assert "idle pre-restart children still present: [111]" in str(excinfo.value)


def test_restart_propagation_bound_fits_inside_the_turnover_budget() -> None:
    # The bound is carved out of the SAME budget the confirmation spends, so a
    # wedge is killed and re-confirmed before the caller's apply window closes.
    budget = 27.0
    bound = prefork._restart_propagation_timeout(budget)
    assert 0.0 < bound < budget
    # What is left over pays for the kill and the confirmation.
    assert budget - bound >= bound
    # An exhausted budget yields no wait at all, never a negative one.
    assert prefork._restart_propagation_timeout(-1.0) == 0.0


def test_kill_guard_spares_the_parent_and_tolerates_a_reaped_child(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    parent = os.getpid()
    reaped, alive = parent + 10, parent + 20
    kills: list[int] = []

    def _kill(pid: int, sig: int) -> None:
        kills.append(pid)
        if pid == reaped:
            raise ProcessLookupError(pid)

    monkeypatch.setattr(prefork.os, "kill", _kill)
    with caplog.at_level(logging.WARNING, logger=prefork.logger.name):
        prefork._kill_pool_children(_HOST, {parent, 0, reaped, alive})
    # The worker parent and a bogus pid are never signalled; an already-reaped
    # child is reported, not fatal.
    assert kills == [reaped, alive]
    assert "already gone" in caplog.text


def test_kill_guard_propagates_an_unexpected_signal_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def _kill(pid: int, sig: int) -> None:
        raise PermissionError(pid)

    monkeypatch.setattr(prefork.os, "kill", _kill)
    with pytest.raises(PermissionError):
        prefork._kill_pool_children(_HOST, {os.getpid() + 30})


# --- registration -------------------------------------------------------------


def test_register_wires_only_the_celery_signals(
    monkeypatch: pytest.MonkeyPatch, stub_app, turnover: prefork.PoolTurnover
) -> None:
    # The setup/ready signals are all this module needs; the post-apply hook that
    # decides WHEN to turn over is the shared backend base's, so registering one
    # here would double-wire it.
    connected: list[Any] = []
    monkeypatch.setattr(prefork.signals.celeryd_after_setup, "connect", lambda r, **_: connected.append(r))
    monkeypatch.setattr(prefork.signals.worker_ready, "connect", lambda r, **_: connected.append(r))
    stub_app.lifecycle.fleet_op_applied.clear()

    turnover.register()

    assert connected == [turnover._record_local_nodename, turnover._mark_worker_ready]
    assert stub_app.lifecycle.fleet_op_applied == []


def _turnover_receiver_counts() -> tuple[int, int]:
    return (
        len(prefork.signals.celeryd_after_setup.receivers or []),
        len(prefork.signals.worker_ready.receivers or []),
    )


def test_a_second_register_connects_no_second_receiver(turnover: prefork.PoolTurnover) -> None:
    turnover.register()
    connected = _turnover_receiver_counts()

    turnover.register()

    assert _turnover_receiver_counts() == connected


def test_signal_receivers_capture_nodename_and_readiness(turnover: prefork.PoolTurnover) -> None:
    turnover._local_nodename = None
    turnover._worker_ready.clear()
    turnover._record_local_nodename(sender="celery@x", instance=None)
    assert turnover._local_nodename == "celery@x"
    turnover._mark_worker_ready(sender=object())
    assert turnover._worker_ready.is_set()


# --- a worker that leaves mid-turnover ------------------------------------------

# A budget long enough that a turnover waiting it out is plainly distinguishable
# from one that ends at the leave.
_LEAVE_BUDGET = 8.0
_PROMPT = 3.0


@pytest.fixture
def leave(runtime: CeleryWorkerRuntime) -> Iterator[Callable[[], None]]:
    """This worker's own leave, as the shutdown drives it: the runtime's warm drain.

    The process-wide ``celery.worker.state`` stop flags the drain sets are restored after.
    """
    from celery.worker import state

    old = (state.should_stop, state.should_terminate)
    state.should_stop = None
    yield runtime.request_drain
    state.should_stop, state.should_terminate = old


def _stopped_consumer_after_restart(monkeypatch: pytest.MonkeyPatch, leave: Callable[[], None]) -> _FakeControl:
    """A worker that answers the pre-restart ``stats``, arms the restart, then leaves.

    Once it leaves its consumer stops, so no later control command is answered.
    """
    control = _install(monkeypatch, [_pool([111]), {}])
    control.on_restart = leave
    return control


def test_a_turnover_in_flight_when_the_worker_leaves_ends_at_once_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    leave: Callable[[], None],
    turnover: prefork.PoolTurnover,
) -> None:
    # The leave stops the consumer that answers the confirmation's polls, and it stops
    # the pool taking new work, so the turnover is abandoned rather than polled out.
    _stopped_consumer_after_restart(monkeypatch, leave)
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=prefork.logger.name):
        turnover.turnover_local_pool("reload_config", _LEAVE_BUDGET)
    assert time.monotonic() - started < _PROMPT
    assert "prefork turnover for reload_config abandoned" in caplog.text
    assert "this worker is leaving" in caplog.text


def test_a_worker_that_leaves_mid_turnover_exits_without_waiting_out_the_turnover(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    leave: Callable[[], None],
    runtime: CeleryWorkerRuntime,
    turnover: prefork.PoolTurnover,
) -> None:
    # The exit itself: the shutdown drains the worker, then cancels the op awaiting the
    # turnover, and the event loop's close joins the thread the turnover runs on. A
    # turnover that kept polling a stopped consumer would hold the process exit for its
    # whole budget.
    control = _install(monkeypatch, [_pool([111]), {}])
    armed = threading.Event()
    released = threading.Event()

    def _hold_the_restart() -> None:
        armed.set()
        released.wait(_PROMPT)

    control.on_restart = _hold_the_restart

    async def _leave_mid_turnover() -> None:
        turning = asyncio.ensure_future(runtime.turn_over_pool(reason="reload_config", budget=_LEAVE_BUDGET))
        assert await asyncio.to_thread(armed.wait, _PROMPT)
        # The drain and the cancel in one loop step, as the shutdown runs them: the
        # turnover's thread cannot report back before its awaiter is cancelled.
        leave()
        turning.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turning
        released.set()

    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=prefork.logger.name):
        asyncio.run(_leave_mid_turnover())
    assert time.monotonic() - started < _PROMPT
    assert "prefork turnover for reload_config abandoned" in caplog.text


def test_a_worker_that_leaves_before_it_starts_consuming_abandons_the_wait(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    leave: Callable[[], None],
    turnover: prefork.PoolTurnover,
) -> None:
    # Setup reached, not yet consuming, then the worker leaves: it will never consume,
    # so the wait for it ends at the leave, not at the budget.
    turnover._worker_ready.clear()
    control = _install(monkeypatch, [_pool([111])])
    timer = threading.Timer(0.2, leave)
    timer.start()
    started = time.monotonic()
    with caplog.at_level(logging.WARNING, logger=prefork.logger.name):
        turnover.turnover_local_pool("reload_config", _LEAVE_BUDGET)
    timer.join()
    assert time.monotonic() - started < _PROMPT
    assert control.broadcasts == []
    assert "this worker is leaving" in caplog.text


def test_a_worker_that_leaves_during_a_wedged_restart_kills_no_child(
    monkeypatch: pytest.MonkeyPatch,
    wedged_restart: threading.Event,
    leave: Callable[[], None],
    turnover: prefork.PoolTurnover,
) -> None:
    # The leave drains the pool itself: the kill guard is for a pool that stays, so a
    # leaving worker's children are left to the drain.
    control = _install(monkeypatch, [_pool([111]), {}])
    control.wedge = wedged_restart
    kills = _record_kills(monkeypatch)
    timer = threading.Timer(0.2, leave)
    timer.start()
    started = time.monotonic()
    turnover.turnover_local_pool("reload_config", _LEAVE_BUDGET)
    timer.join()
    assert time.monotonic() - started < _PROMPT
    assert kills == []


def test_a_turnover_that_starts_after_the_leave_sends_no_control_command(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    leave: Callable[[], None],
    turnover: prefork.PoolTurnover,
) -> None:
    control = _install(monkeypatch, [_pool([111])])
    leave()
    with caplog.at_level(logging.WARNING, logger=prefork.logger.name):
        turnover.turnover_local_pool("reload_config", _LEAVE_BUDGET)
    assert control.broadcasts == []
    assert control.connections == []
    assert "this worker is leaving" in caplog.text


def test_a_pool_read_the_leave_left_unanswered_abandons_the_turnover(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    leave: Callable[[], None],
    turnover: prefork.PoolTurnover,
) -> None:
    # The worker leaves while the pre-restart ``stats`` is in flight, so nothing answers
    # it: the leave is why, not a broken pool, and the op is not failed for it.
    control = _install(monkeypatch, [{}])
    control.on_inspect = leave
    with caplog.at_level(logging.WARNING, logger=prefork.logger.name):
        turnover.turnover_local_pool("reload_config", _LEAVE_BUDGET)
    assert control.broadcasts == []
    assert "prefork turnover for reload_config abandoned" in caplog.text
    assert "did not answer stats" in caplog.text


def test_a_restart_the_leave_left_unanswered_abandons_the_turnover(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    leave: Callable[[], None],
    turnover: prefork.PoolTurnover,
) -> None:
    # The worker leaves while the restart is in flight, and the restart then fails for
    # want of a reply. The restart runs on its own thread, so it leaves a beat after the
    # turnover started waiting on it; the leave checks run far apart here, so that wait
    # ends on the restart's own failure, which the report names.
    monkeypatch.setattr(prefork, "_LEAVE_CHECK_INTERVAL", _LEAVE_BUDGET)
    control = _install(monkeypatch, [_pool([111])])
    control.restart_reply = []

    def _leave_mid_restart() -> None:
        time.sleep(0.5)
        leave()

    control.on_restart = _leave_mid_restart
    with caplog.at_level(logging.WARNING, logger=prefork.logger.name):
        turnover.turnover_local_pool("reload_config", _LEAVE_BUDGET)
    assert "prefork turnover for reload_config abandoned" in caplog.text
    assert "pool restart request failed" in caplog.text
