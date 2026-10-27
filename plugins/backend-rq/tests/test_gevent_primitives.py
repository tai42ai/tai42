"""The gevent pool's building blocks, exercised in this process without patching it.

The pool's job budget is a ``gevent.Timeout`` raised into the job's greenlet; its OS-thread
primitives are looked up through ``gevent.monkey`` once that module is loaded, so they stay
the unpatched originals; and ``setup_gevent`` patches the process through gevent's own
``patch_all``. The end-to-end pool runs against a real Redis in ``test_gevent_pool``.
"""

from __future__ import annotations

import sys
import time
from types import ModuleType

import gevent
import pytest
from rq.timeouts import JobTimeoutException

from tai42_backend_rq import worker as worker_module
from tai42_backend_rq import worker_pools


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
        worker_pools._GeventDeathPenalty(0.05, JobTimeoutException),
    ):
        gevent.sleep(2)

    assert time.monotonic() - started < 1.0


def test_the_gevent_penalty_is_closed_when_the_job_ends_inside_its_budget() -> None:
    penalty = worker_pools._GeventDeathPenalty(0.05, JobTimeoutException)

    with penalty:
        gevent.sleep(0)

    assert penalty._gevent_timeout is None
    gevent.sleep(0.1)  # past the budget: a timeout left running would raise here


def test_a_gevent_penalty_without_a_budget_arms_nothing() -> None:
    penalty = worker_pools._GeventDeathPenalty(0, JobTimeoutException)

    with penalty:
        assert penalty._gevent_timeout is None
        gevent.sleep(0.02)


def test_the_gevent_pools_penalty_records_the_section_it_guards() -> None:
    """The refresher reads the record to tell a section still running from one the penalty has ended."""
    with worker_pools._WatchedGeventDeathPenalty(30, JobTimeoutException, job_id="job-1"):
        deadline, timeout = worker_pools._WatchedDeathPenalty._armed["job-1"]
        assert timeout == 30
        assert deadline > time.monotonic()

    assert "job-1" not in worker_pools._WatchedDeathPenalty._armed


def test_once_gevent_monkey_is_loaded_the_originals_come_from_it(monkeypatch: pytest.MonkeyPatch) -> None:
    lookups: list[tuple[str, str]] = []
    unpatched = object()

    def _get_original(module: str, name: str) -> object:
        lookups.append((module, name))
        return unpatched

    monkey = ModuleType("gevent.monkey")
    monkey.get_original = _get_original  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gevent.monkey", monkey)

    assert worker_pools._original("_thread", "start_new_thread") is unpatched
    assert lookups == [("_thread", "start_new_thread")]


def test_without_gevent_monkey_the_originals_are_the_attributes_as_they_stand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "gevent.monkey", raising=False)

    assert worker_pools._original("time", "monotonic") is time.monotonic


def test_setup_gevent_patches_the_process_through_gevents_patch_all(monkeypatch: pytest.MonkeyPatch) -> None:
    patched: list[str] = []
    monkey = ModuleType("gevent.monkey")
    monkey.patch_all = lambda: patched.append("patch_all")  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "gevent.monkey", monkey)
    monkeypatch.setattr(gevent, "monkey", monkey, raising=False)

    worker_module.setup_gevent()

    assert patched == ["patch_all"]
