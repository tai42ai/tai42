"""The gevent pool's building blocks, exercised in this process without patching it.

The pool's job budget is a timer on the job's hub that throws the job's timeout into the job's
greenlet; its OS-thread
primitives are looked up through ``gevent.monkey`` once that module is loaded, so they stay
the unpatched originals. The end-to-end pool runs against a real Redis in ``test_gevent_pool``.
"""

from __future__ import annotations

import sys
import time
from types import ModuleType

import gevent
import gevent.event
import pytest
from rq.timeouts import JobTimeoutException

from tai42_backend_rq import death_penalties, unpatched


@pytest.fixture
def gevent_pool_timeout_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """``JobTimeoutException`` with its own constructor, as the gevent pool's process holds it.

    Building rq's ``TimerDeathPenalty`` (the solo pool's) replaces ``JobTimeoutException.__init__``
    process-wide with one that writes that timer's timeout into every later message. The gevent pool
    builds every penalty from its own class, so its process keeps the inherited constructor; earlier
    tests in this process may have built a timer penalty, so it is restored here for the test.
    """
    if "__init__" in vars(JobTimeoutException):
        monkeypatch.delattr(JobTimeoutException, "__init__")


@pytest.mark.usefixtures("gevent_pool_timeout_exception")
def test_the_gevent_penalty_raises_the_jobs_timeout_when_the_job_yields_past_its_budget() -> None:
    started = time.monotonic()

    with (
        pytest.raises(JobTimeoutException, match=r"^Task exceeded maximum timeout value \(0\.05 seconds\)$"),
        death_penalties.WatchedGeventDeathPenalty(0.05, JobTimeoutException),
    ):
        gevent.sleep(2)

    assert time.monotonic() - started < 1.0


def test_the_gevent_penalty_is_closed_when_the_job_ends_inside_its_budget() -> None:
    penalty = death_penalties.WatchedGeventDeathPenalty(0.05, JobTimeoutException)

    with penalty:
        gevent.sleep(0)

    assert penalty._budget_timer is None
    gevent.sleep(0.1)  # past the budget: a timer left running would throw here


def test_a_gevent_penalty_without_a_budget_arms_nothing() -> None:
    penalty = death_penalties.WatchedGeventDeathPenalty(0, JobTimeoutException)

    with penalty:
        assert penalty._budget_timer is None
        gevent.sleep(0.02)


def test_the_gevent_pools_penalty_records_the_section_it_guards() -> None:
    """The refresher reads the record to tell a section still running from one the penalty has ended."""
    with death_penalties.WatchedGeventDeathPenalty(30, JobTimeoutException, job_id="job-1"):
        deadline, timeout = death_penalties.WatchedDeathPenalty._armed["job-1"]
        assert timeout == 30
        assert deadline > time.monotonic()

    assert "job-1" not in death_penalties.WatchedDeathPenalty._armed


@pytest.mark.usefixtures("gevent_pool_timeout_exception")
def test_a_gevent_section_that_caught_its_budget_s_timeout_takes_no_stop_and_stays_overdue() -> None:
    """The budget's timer closes the section as it throws: a stop then finds no section, and its deadline stays."""
    caught = gevent.event.Event()
    raised: list[str] = []
    stopped: list[bool] = []
    overdue: list[float | None] = []

    def job() -> None:
        try:
            with death_penalties.WatchedGeventDeathPenalty(0.05, JobTimeoutException, job_id="job-g"):
                try:
                    gevent.sleep(2)
                except JobTimeoutException:
                    raised.append("timeout")
                caught.set()
                gevent.sleep(0.1)  # a stop thrown in would land here
                overdue.append(death_penalties.WatchedDeathPenalty.overdue("job-g", margin=0))
        except BaseException as stray:
            raised.append(f"stray {type(stray).__name__}")

    def stop() -> None:
        caught.wait(timeout=2)
        stopped.append(death_penalties.WatchedDeathPenalty.interrupt("job-g"))
        stopped.append(death_penalties.WatchedDeathPenalty.interrupt("job-g"))

    gevent.joinall([gevent.spawn(job), gevent.spawn(stop)], timeout=5)

    assert raised == ["timeout"]
    assert stopped == [False, False]
    assert overdue == [0.05]
    assert "job-g" not in death_penalties.WatchedDeathPenalty._armed


def test_a_gevent_section_that_caught_a_stop_gets_no_timeout_at_its_budget_and_stays_overdue() -> None:
    """A thrown stop closes the section: its budget's timer is disarmed, and its deadline stays recorded."""
    raised: list[str] = []
    stopped: list[bool] = []
    overdue: list[float | None] = []

    def job() -> None:
        try:
            with death_penalties.WatchedGeventDeathPenalty(0.05, JobTimeoutException, job_id="job-s"):
                try:
                    gevent.sleep(2)
                except death_penalties.JobStopped:
                    raised.append("stop")
                gevent.sleep(0.2)  # past the budget: a timer left armed would throw here
                overdue.append(death_penalties.WatchedDeathPenalty.overdue("job-s", margin=0))
        except BaseException as stray:
            raised.append(f"stray {type(stray).__name__}")

    def stop() -> None:
        stopped.append(death_penalties.WatchedDeathPenalty.interrupt("job-s"))

    gevent.joinall([gevent.spawn(job), gevent.spawn(stop)], timeout=5)

    assert raised == ["stop"]
    assert stopped == [True]
    assert overdue == [0.05]
    assert death_penalties.WatchedDeathPenalty.interrupt("job-s") is False


def test_a_budget_timer_that_fires_once_its_section_has_ended_throws_nothing() -> None:
    """A fire of a timer the section's end has disarmed finds the section ended and leaves the job alone."""
    penalty = death_penalties.WatchedGeventDeathPenalty(30, JobTimeoutException, job_id="job-late")

    with penalty:
        timer = penalty._budget_timer

    penalty._budget_fired(timer)
    gevent.sleep(0.02)


def test_once_gevent_monkey_is_loaded_the_originals_come_from_it(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups: list[tuple[str, str]] = []
    gevent_original = object()

    def _get_original(module: str, name: str) -> object:
        lookups.append((module, name))
        return gevent_original

    monkey = ModuleType("gevent.monkey")
    monkey.get_original = _get_original  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gevent.monkey", monkey)

    assert unpatched.original("_thread", "start_new_thread") is gevent_original
    assert lookups == [("_thread", "start_new_thread")]


def test_without_gevent_monkey_the_originals_are_the_attributes_as_they_stand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "gevent.monkey", raising=False)

    assert unpatched.original("time", "monotonic") is time.monotonic
