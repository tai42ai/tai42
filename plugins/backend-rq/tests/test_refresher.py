"""The non-forking pools' refresher, run on the test's own thread.

``execute_job`` starts the refresher on a new OS thread beside the in-process job; here its two
bodies run directly: ``_refresh_until`` (the thread's own Redis client, its cleanup, the release
of ``ended``) and ``_maintain_heartbeats_until`` (one report per tick, rq's heartbeats once per
monitoring interval, and the stop once the guarded section runs past its timeout).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import pytest
from redis import Redis
from rq.timeouts import JobTimeoutException

from tai42_backend_rq import worker_pools


class _ClosingRedis(Redis):
    """A Redis client that records whether it was closed (it never connects: nothing here sends a command)."""

    closed = False

    def close(self) -> None:
        self.closed = True
        super().close()


class _Reports:
    """A progress reporter that records each promise and releases ``done`` after ``stop_after`` reports."""

    def __init__(self, done: Any = None, stop_after: int = 0) -> None:
        self.promises: list[float] = []
        self._done = done
        self._stop_after = stop_after

    def __call__(self, *, next_within: float) -> None:
        self.promises.append(next_within)
        if self._done is not None and len(self.promises) == self._stop_after:
            self._done.release()


def _held_lock() -> Any:
    lock = worker_pools._os_thread_lock()
    lock.acquire()
    return lock


def _refresher_worker(reports: Any, *, interval: float, running: bool = True) -> Any:
    """A solo-pool worker holding only what the refresher reads: name, interval, reporter, execution."""
    worker: Any = object.__new__(worker_pools.CustomRQSimpleWorker)
    worker.name = "w1"
    worker.job_monitoring_interval = interval
    worker._report_progress = reports
    worker._execution_guard = worker_pools._os_thread_lock()
    worker.execution = SimpleNamespace(job_id="job-1") if running else None
    worker.connection = _ClosingRedis(host="127.0.0.1", port=6390, db=3, socket_timeout=2.0)
    return worker


_JOB = SimpleNamespace(id="job-1")


def _record_refreshes(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Stand in for rq's heartbeat refresh: each call records the job it refreshed."""
    refreshes: list[Any] = []
    monkeypatch.setattr(
        worker_pools.CustomRQSimpleWorker, "maintain_heartbeats", lambda self, job: refreshes.append(job)
    )
    return refreshes


@pytest.fixture(autouse=True)
def _fast_ticks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(worker_pools, "_REFRESH_TICK_SECONDS", 0.01)


# --- the refresher thread's own client ---------------------------------------


def test_the_refresher_refreshes_rq_on_a_client_of_its_own_and_closes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    done, ended = _held_lock(), _held_lock()
    worker = _refresher_worker(_Reports(), interval=0)
    work_loop = worker.connection
    seen: list[Any] = []

    def _refresh(self: Any, job: Any) -> None:
        seen.extend([self.connection, self.connection])
        done.release()

    monkeypatch.setattr(worker_pools.CustomRQSimpleWorker, "maintain_heartbeats", _refresh)

    worker._refresh_until(_JOB, done, ended)

    client, again = seen
    assert again is client, "the refresher built a second client"
    assert client is not work_loop
    assert type(client) is _ClosingRedis
    assert client.connection_pool is not work_loop.connection_pool
    assert type(client.connection_pool) is type(work_loop.connection_pool)
    assert client.connection_pool.connection_class is work_loop.connection_pool.connection_class
    assert client.connection_pool.max_connections == work_loop.connection_pool.max_connections
    assert client.connection_pool.connection_kwargs == work_loop.connection_pool.connection_kwargs
    assert client.closed
    assert not work_loop.closed
    assert ended.acquire(blocking=False), "the refresher did not release ended"
    assert worker.connection is work_loop, "this thread still reads the refresher's client"


def test_the_refresher_closes_its_client_and_releases_ended_when_its_loop_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done, ended = _held_lock(), _held_lock()
    worker = _refresher_worker(_Reports(), interval=0)
    work_loop = worker.connection
    built: list[Any] = []

    def _break(self: Any, job: Any, done: Any) -> None:
        built.append(self.connection)
        raise RuntimeError("refresh loop broke")

    monkeypatch.setattr(worker_pools.CustomRQSimpleWorker, "_maintain_heartbeats_until", _break)

    with pytest.raises(RuntimeError, match="refresh loop broke"):
        worker._refresh_until(_JOB, done, ended)

    assert built[0].closed
    assert ended.acquire(blocking=False)
    assert worker.connection is work_loop


def test_a_refresher_that_never_refreshed_builds_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    done, ended = _held_lock(), _held_lock()
    done.release()
    worker = _refresher_worker(_Reports(), interval=0)
    built: list[Any] = []
    monkeypatch.setattr(worker_pools, "_client_like", lambda connection: built.append(connection))

    worker._refresh_until(_JOB, done, ended)

    assert built == []
    assert ended.acquire(blocking=False)


# --- the refresh loop ---------------------------------------------------------


def test_each_tick_reports_and_rqs_heartbeats_wait_for_the_monitoring_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done = _held_lock()
    reports = _Reports(done, stop_after=5)
    worker = _refresher_worker(reports, interval=60)
    refreshes = _record_refreshes(monkeypatch)

    worker._maintain_heartbeats_until(_JOB, done)

    assert reports.promises == [0.01] * 5
    assert refreshes == []


def test_a_tick_past_the_monitoring_interval_refreshes_rqs_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    done = _held_lock()
    reports = _Reports(done, stop_after=3)
    worker = _refresher_worker(reports, interval=0)
    refreshes = _record_refreshes(monkeypatch)

    worker._maintain_heartbeats_until(_JOB, done)

    assert refreshes == [_JOB, _JOB, _JOB]


def test_once_rq_has_ended_the_execution_the_ticks_only_report(monkeypatch: pytest.MonkeyPatch) -> None:
    done = _held_lock()
    reports = _Reports(done, stop_after=3)
    worker = _refresher_worker(reports, interval=0, running=False)
    refreshes = _record_refreshes(monkeypatch)

    worker._maintain_heartbeats_until(_JOB, done)

    assert len(reports.promises) == 3
    assert refreshes == []


def test_a_failed_refresh_is_logged_at_error_and_retried_on_the_next_tick(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    done = _held_lock()
    reports = _Reports(done, stop_after=3)
    worker = _refresher_worker(reports, interval=0)
    attempts: list[Any] = []

    def _fail(self: Any, job: Any) -> None:
        attempts.append(job)
        raise ConnectionError("redis went away")

    monkeypatch.setattr(worker_pools.CustomRQSimpleWorker, "maintain_heartbeats", _fail)

    with caplog.at_level("ERROR", logger="tai42_backend_rq.worker_pools"):
        worker._maintain_heartbeats_until(_JOB, done)

    assert len(attempts) == 3
    failures = [r for r in caplog.records if r.name == "tai42_backend_rq.worker_pools"]
    assert [r.getMessage() for r in failures] == ["worker 'w1': heartbeat refresh failed while running job-1"] * 3
    assert all(r.levelname == "ERROR" and r.exc_info is not None for r in failures)


def test_a_section_running_past_its_timeout_stops_the_refresher_without_reporting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    done = _held_lock()
    reports = _Reports()
    worker = _refresher_worker(reports, interval=0)
    refreshes = _record_refreshes(monkeypatch)
    monkeypatch.setitem(worker_pools._WatchedDeathPenalty._armed, "job-1", (time.monotonic() - 1.0, 0.2))

    with caplog.at_level("ERROR", logger="tai42_backend_rq.worker_pools"):
        worker._maintain_heartbeats_until(_JOB, done)  # returns on its own: ``done`` is still held

    assert reports.promises == []
    assert refreshes == []
    assert [r.getMessage() for r in caplog.records] == [
        "worker 'w1': job job-1 is still running past its 0.2s timeout, which this pool cannot end; "
        "it reports no more progress, so the liveness probe ends this worker"
    ]


# --- the penalty record the refresher reads ----------------------------------


def test_a_job_with_no_guarded_section_is_never_overdue() -> None:
    assert worker_pools._WatchedDeathPenalty.overdue("no-such-job", margin=0) is None


def test_a_guarded_section_inside_its_deadline_and_margin_is_not_overdue() -> None:
    with worker_pools._WatchedTimerDeathPenalty(30, JobTimeoutException, job_id="job-1"):
        assert worker_pools._WatchedDeathPenalty.overdue("job-1", margin=1.0) is None


def test_a_guarded_section_past_its_deadline_but_inside_the_margin_is_not_overdue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(worker_pools._WatchedDeathPenalty._armed, "job-1", (time.monotonic() - 0.5, 2.0))

    assert worker_pools._WatchedDeathPenalty.overdue("job-1", margin=60.0) is None


def test_a_guarded_section_past_its_deadline_and_margin_is_overdue_by_its_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(worker_pools._WatchedDeathPenalty._armed, "job-1", (time.monotonic() - 5.0, 2.0))

    assert worker_pools._WatchedDeathPenalty.overdue("job-1", margin=1.0) == 2.0
