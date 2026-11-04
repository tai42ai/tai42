"""The ``solo`` pool's penalty at the edges of the section it guards, with a stop or its timer landing there.

Each test forces one interleaving of the job's thread (the test's own) with a stop or a timer on
another thread, and then waits past the section's budget: a timer left armed would raise the job's
timeout into the test's thread there, and a raise left over would land there.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from rq.timeouts import JobTimeoutException

from tai42_backend_rq.death_penalties import JobStopped, WatchedDeathPenalty, WatchedTimerDeathPenalty

_JOB = "job-edge"
_BUDGET_SECONDS = 0.3


def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.005)


def _runs_in(thread_id: int, function: str) -> bool:
    """Whether the innermost frame of the thread ``thread_id`` runs ``function``."""
    frame = sys._current_frames().get(thread_id)
    return frame is not None and frame.f_code.co_name == function


def _nothing_lands_past_the_budget() -> None:
    """Wait out the budget and a margin in short steps; a stray timeout or stop fails the test."""
    try:
        for _ in range(int(3 * _BUDGET_SECONDS / 0.02)):
            time.sleep(0.02)
    except (JobTimeoutException, JobStopped) as stray:
        pytest.fail(f"raised into the job's thread after its section ended: {type(stray).__name__}: {stray}")


def _start(target: Callable[[], Any]) -> threading.Thread:
    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread


def test_a_stop_delivered_while_the_section_ends_leaves_nothing_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop holds the guard while the job's thread, its body done, waits on it to end the section.

    The stop is taken as the section's end, and the section's timer is disarmed with it.
    """
    penalty = WatchedTimerDeathPenalty(_BUDGET_SECONDS, JobTimeoutException, job_id=_JOB)
    job_thread = threading.get_ident()
    delivering = threading.Event()
    deliver = penalty._deliver

    def deliver_once_the_section_ends(stop: type[JobStopped]) -> None:
        delivering.set()
        _until(lambda: _runs_in(job_thread, "cancel_death_penalty"))
        deliver(stop)

    monkeypatch.setattr(penalty, "_deliver", deliver_once_the_section_ends)
    stopped: list[bool] = []

    with penalty:
        stop = _start(lambda: stopped.append(WatchedDeathPenalty.interrupt(_JOB)))
        delivering.wait(timeout=5)

    stop.join(timeout=5)
    _nothing_lands_past_the_budget()
    assert stopped == [True]
    assert penalty._timer is None
    assert WatchedDeathPenalty.overdue(_JOB, margin=0) is None


class _GuardThatHoldsTheEntry:
    """The penalties' guard, whose first release by the job's thread waits until a stop has been delivered."""

    def __init__(self, guard: Any, job_thread: int) -> None:
        self._guard = guard
        self._job_thread = job_thread
        self.opened = threading.Event()
        self.delivered = threading.Event()

    def __enter__(self) -> None:
        self._guard.acquire()

    def __exit__(self, *exc_info: object) -> None:
        self._guard.release()
        if threading.get_ident() == self._job_thread and not self.opened.is_set():
            self.opened.set()
            self.delivered.wait(timeout=5)


def test_a_stop_delivered_as_the_section_opens_leaves_nothing_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stop lands in the job's thread once the section may be stopped, before the body starts.

    The job is stopped before its body runs, and the section's timer and record go with it.
    """
    guard = _GuardThatHoldsTheEntry(WatchedDeathPenalty._raise_guard, threading.get_ident())
    monkeypatch.setattr(WatchedDeathPenalty, "_raise_guard", guard)
    penalty = WatchedTimerDeathPenalty(_BUDGET_SECONDS, JobTimeoutException, job_id=_JOB)
    stopped: list[bool] = []

    def stop_once_open() -> None:
        guard.opened.wait(timeout=5)
        stopped.append(WatchedDeathPenalty.interrupt(_JOB))
        guard.delivered.set()

    stop = _start(stop_once_open)
    body_ran = False
    with pytest.raises(JobStopped), penalty:
        body_ran = True

    stop.join(timeout=5)
    _nothing_lands_past_the_budget()
    assert stopped == [True]
    assert not body_ran
    assert penalty._timer is None
    assert WatchedDeathPenalty.overdue(_JOB, margin=0) is None


def test_a_timer_that_fires_once_its_section_has_ended_raises_nothing() -> None:
    """rq's timer may run its callback after the section's end cancelled it; the callback finds the section ended."""
    penalty = WatchedTimerDeathPenalty(30, JobTimeoutException, job_id=_JOB)

    with penalty:
        pass

    late = _start(penalty.handle_death_penalty)
    _nothing_lands_past_the_budget()
    late.join(timeout=5)


def _runs_until(stop_waiting: Callable[[], bool], timeout: float = 5.0) -> None:
    """Run Python bytecode in the job's thread until ``stop_waiting`` holds, so a raise lands here."""
    deadline = time.monotonic() + timeout
    while not stop_waiting():
        if time.monotonic() > deadline:
            pytest.fail("nothing was raised into the section")
        time.sleep(0.005)


def _stop_when_told(stopped: list[bool]) -> tuple[threading.Thread, threading.Event]:
    """A thread that stops ``_JOB`` once the returned event is set, and records whether a section took it."""
    told = threading.Event()

    def stop() -> None:
        told.wait(timeout=5)
        stopped.append(WatchedDeathPenalty.interrupt(_JOB))

    return _start(stop), told


def test_a_section_that_caught_its_budget_s_timeout_takes_no_stop_and_stays_overdue() -> None:
    """The budget's raise closes the section: a stop then finds no section, and its deadline stays recorded."""
    penalty = WatchedTimerDeathPenalty(_BUDGET_SECONDS, JobTimeoutException, job_id=_JOB)
    stopped: list[bool] = []
    stop, told = _stop_when_told(stopped)

    with penalty:
        with pytest.raises(JobTimeoutException):
            _runs_until(lambda: False)
        told.set()
        stop.join(timeout=5)
        _nothing_lands_past_the_budget()
        overdue = WatchedDeathPenalty.overdue(_JOB, margin=0)

    assert stopped == [False]
    assert overdue == _BUDGET_SECONDS
    assert penalty._timer is None
    assert WatchedDeathPenalty.overdue(_JOB, margin=0) is None


def test_a_section_that_caught_a_stop_gets_no_timeout_at_its_budget_and_stays_overdue() -> None:
    """A raised stop disarms the budget; the section's deadline stays recorded for the worker's refresher."""
    penalty = WatchedTimerDeathPenalty(_BUDGET_SECONDS, JobTimeoutException, job_id=_JOB)
    stopped: list[bool] = []
    stop, told = _stop_when_told(stopped)

    def runs_once_it_has_told_the_stop() -> None:
        told.set()
        _runs_until(lambda: False)

    with penalty:
        with pytest.raises(JobStopped):
            runs_once_it_has_told_the_stop()
        _nothing_lands_past_the_budget()
        overdue = WatchedDeathPenalty.overdue(_JOB, margin=0)

    stop.join(timeout=5)
    assert stopped == [True]
    assert overdue == _BUDGET_SECONDS
    assert WatchedDeathPenalty.interrupt(_JOB) is False
