"""The rq worker reports its loop's progress: every heartbeat, every horse-monitor tick, every refresher tick.

The prefork parent reports from its own loop (``heartbeat`` on each dequeue poll and job
boundary, and each ``wait_for_horse`` tick while a horse runs); the forked horse never
reports, since the liveness file is the parent's signal. The non-forking pools report from
the refresher thread that runs beside an in-process job.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fakeredis
import pytest
from rq import Queue, SimpleWorker, Worker
from rq.timeouts import JobTimeoutException
from tai42_contract.backend.runtime import ProgressLiveness

from tai42_backend_rq import worker as worker_module
from tai42_backend_rq import worker_pools


class _Reports:
    """A progress reporter that records every promise it receives."""

    def __init__(self) -> None:
        self.promises: list[float] = []

    @property
    def count(self) -> int:
        return len(self.promises)

    def __call__(self, *, next_within: float) -> None:
        self.promises.append(next_within)


def _bare(cls: type[Any], reports: _Reports) -> Any:
    """A worker of ``cls`` with only the reporter set, as a forked or unit-driven worker holds it."""
    worker: Any = object.__new__(cls)
    worker._report_progress = reports
    worker._is_work_horse = False
    worker.execution = None
    if issubclass(cls, worker_pools.CustomRQSimpleWorker):
        worker._execution_guard = worker_pools._os_thread_lock()
    return worker


def _begin_execution(worker: Any, job: Any) -> None:
    """Stand in for rq's ``prepare_execution``: the job's execution exists until rq ends it."""
    worker.execution = SimpleNamespace(job_id=job.id)


@pytest.mark.parametrize("cls", [worker_pools.CustomRQWorker, worker_pools.CustomRQSimpleWorker])
def test_both_worker_classes_take_the_reporter_at_construction(cls: type[Any]) -> None:
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    reports = _Reports()

    worker = cls([Queue("q", connection=conn)], connection=conn, report_progress=reports)

    worker.heartbeat()
    assert reports.promises == [worker_pools._DEQUEUE_POLL_SECONDS]


@pytest.mark.parametrize(
    ("cls", "base"), [(worker_pools.CustomRQWorker, Worker), (worker_pools.CustomRQSimpleWorker, SimpleWorker)]
)
def test_a_heartbeat_reports_progress_before_rqs_own(
    monkeypatch: pytest.MonkeyPatch, cls: type[Any], base: type[Any]
) -> None:
    reports = _Reports()
    seen: list[int] = []
    monkeypatch.setattr(base, "heartbeat", lambda self, timeout=None, pipeline=None: seen.append(reports.count))
    worker = _bare(cls, reports)

    worker.heartbeat()

    assert seen == [1]
    assert reports.promises == [worker_pools._DEQUEUE_POLL_SECONDS]


def test_the_work_horse_never_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    """rq's horse heartbeats while it prepares the job; the file belongs to the parent, so the horse is silent."""
    reports = _Reports()
    rq_beats: list[str] = []
    monkeypatch.setattr(Worker, "heartbeat", lambda self, timeout=None, pipeline=None: rq_beats.append("beat"))
    monkeypatch.setattr(Worker, "main_work_horse", lambda self, job, queue: self.heartbeat())
    worker = _bare(worker_pools.CustomRQWorker, reports)

    worker.main_work_horse(object(), object())

    assert rq_beats == ["beat"]
    assert reports.count == 0


def test_the_horse_monitor_reports_on_every_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    """While a horse runs the parent sits in ``wait_for_horse``; each poll tick is its loop's progress."""
    reports = _Reports()
    worker = _bare(worker_pools.CustomRQWorker, reports)
    worker.job_monitoring_interval = 0.2
    worker._horse_pid = 424242
    worker._killed_horse_pid = 0
    monkeypatch.setattr(worker_pools, "_HORSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker_pools.os, "wait4", lambda pid, flags: (0, 0, None))

    with pytest.raises(worker_pools.HorseMonitorTimeoutException):
        worker.wait_for_horse()

    assert reports.count >= 10
    assert set(reports.promises) == {0.01}


def test_the_simple_worker_refresher_reports_every_tick_and_refreshes_rq_on_its_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports = _Reports()
    worker = _bare(worker_pools.CustomRQSimpleWorker, reports)
    worker.job_monitoring_interval = 0.1
    refreshes: list[float] = []
    monkeypatch.setattr(worker_pools, "_REFRESH_TICK_SECONDS", 0.01)
    monkeypatch.setattr(type(worker), "maintain_heartbeats", lambda self, job: refreshes.append(time.monotonic()))
    monkeypatch.setattr(type(worker), "prepare_execution", _begin_execution)
    monkeypatch.setattr(type(worker), "set_state", lambda self, state: None)
    monkeypatch.setattr(type(worker), "perform_job", lambda self, job, queue: time.sleep(0.35) or True)

    worker.execute_job(SimpleNamespace(id="job-1", timeout=300), object())

    assert reports.count >= 15, "the refresher did not report progress on its tick"
    assert set(reports.promises) == {0.01}
    assert 2 <= len(refreshes) <= 4, f"rq's heartbeat cadence changed: {len(refreshes)} refreshes"


def test_a_failed_refresher_report_is_logged_and_retried(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worker = _bare(worker_pools.CustomRQSimpleWorker, _Reports())
    worker.name = "w1"
    worker.job_monitoring_interval = 5.0

    def _refuse(*, next_within: float) -> None:
        raise RuntimeError("liveness file is owned by live process 1")

    worker._report_progress = _refuse
    monkeypatch.setattr(worker_pools, "_REFRESH_TICK_SECONDS", 0.01)
    monkeypatch.setattr(type(worker), "prepare_execution", _begin_execution)
    monkeypatch.setattr(type(worker), "set_state", lambda self, state: None)
    monkeypatch.setattr(type(worker), "perform_job", lambda self, job, queue: time.sleep(0.1) or True)

    with caplog.at_level("ERROR", logger="tai42_backend_rq.worker_pools"):
        worker.execute_job(SimpleNamespace(id="job-1", timeout=300), object())

    failures = [r for r in caplog.records if "reporting its progress failed" in r.getMessage()]
    assert len(failures) >= 2


def test_the_worker_runtime_declares_progress_liveness() -> None:
    assert worker_module.RqWorkerRuntime.liveness == ProgressLiveness()


def test_the_standalone_entry_writes_the_liveness_file_while_it_works_and_removes_it(
    monkeypatch: pytest.MonkeyPatch, liveness_path: Path
) -> None:
    seen: dict[str, Any] = {}

    class _Worker:
        def __init__(self, queues: Any, *, name: Any, connection: Any, default_result_ttl: int, report_progress: Any):
            self._report = report_progress

        def work(self, **kwargs: Any) -> None:
            self._report(next_within=worker_pools._DEQUEUE_POLL_SECONDS)
            seen["during"] = liveness_path.read_text()

        def raise_for_liveness_failure(self) -> None:
            seen["checked"] = True

    conn = SimpleNamespace(close=lambda: seen.__setitem__("closed", True))
    monkeypatch.setattr(worker_module, "install_fork_hooks", lambda: None)
    monkeypatch.setattr(worker_module, "rq_redis_connection", lambda redis_url: conn)
    monkeypatch.setattr(worker_module, "CustomRQSimpleWorker", _Worker)

    worker_module.start_rq_worker(None, None, "info", False, 500, "solo", None)

    assert seen["during"] == f"{os.getpid()}\n10\n"
    assert seen["closed"] is True
    assert seen["checked"] is True
    assert not liveness_path.exists()


def test_the_standalone_entry_removes_the_liveness_file_when_the_work_loop_raises(
    monkeypatch: pytest.MonkeyPatch, liveness_path: Path
) -> None:
    class _Worker:
        def __init__(self, queues: Any, *, name: Any, connection: Any, default_result_ttl: int, report_progress: Any):
            self._report = report_progress

        def work(self, **kwargs: Any) -> None:
            self._report(next_within=worker_pools._DEQUEUE_POLL_SECONDS)
            raise RuntimeError("work loop failed")

    monkeypatch.setattr(worker_module, "install_fork_hooks", lambda: None)
    monkeypatch.setattr(worker_module, "rq_redis_connection", lambda redis_url: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(worker_module, "CustomRQSimpleWorker", _Worker)

    with pytest.raises(RuntimeError, match="work loop failed"):
        worker_module.start_rq_worker(None, None, "info", False, 500, "solo", None)

    assert not liveness_path.exists()


def _perform_blocked_in_c(worker: Any, job: Any, queue: Any) -> bool:
    """rq's guarded job body, blocked in a C call (``time.sleep``) past its timeout: the timer's exception waits."""
    try:
        with worker.death_penalty_class(job.timeout, JobTimeoutException, job_id=job.id):
            time.sleep(0.6)
    except JobTimeoutException:
        return False
    return True


def test_the_refresher_stops_reporting_once_the_job_runs_past_its_budget(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A job blocked where the timer penalty cannot reach runs on; past its budget the worker reports nothing."""
    stamps: list[float] = []
    reports = _Reports()
    worker = _bare(worker_pools.CustomRQSimpleWorker, reports)
    worker.name = "w1"
    worker.job_monitoring_interval = 0.05
    refreshes: list[float] = []

    def _record(*, next_within: float) -> None:
        stamps.append(time.monotonic())
        reports(next_within=next_within)

    worker._report_progress = _record
    monkeypatch.setattr(worker_pools, "_REFRESH_TICK_SECONDS", 0.01)
    monkeypatch.setattr(type(worker), "maintain_heartbeats", lambda self, job: refreshes.append(time.monotonic()))
    monkeypatch.setattr(type(worker), "prepare_execution", _begin_execution)
    monkeypatch.setattr(type(worker), "set_state", lambda self, state: None)
    monkeypatch.setattr(type(worker), "perform_job", _perform_blocked_in_c)
    job = SimpleNamespace(id="job-1", timeout=0.2)

    started = time.monotonic()
    with caplog.at_level("ERROR", logger="tai42_backend_rq.worker_pools"):
        worker.execute_job(job, object())

    assert stamps, "the refresher never reported while the job ran inside its budget"
    assert max(stamps) - started < 0.2 + worker_pools._REFRESH_TICK_SECONDS * 5, "reports went on past the budget"
    assert all(stamp - started < 0.3 for stamp in refreshes), "rq's heartbeats went on past the budget"
    assert [record.getMessage() for record in caplog.records] == [
        "worker 'w1': job job-1 is still running past its 0.2s timeout, which this pool cannot end; "
        "it reports no more progress, so the liveness probe ends this worker"
    ]


def _hold_the_python_loop(seconds: float) -> None:
    """A job body that runs Python code, which the timer penalty can interrupt, for ``seconds``."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


def test_a_job_the_timer_ends_at_its_budget_is_not_reported_as_running_past_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from tai42_kit.settings import reset_all_settings

    monkeypatch.setenv("RQ_TASK_TIMEOUT", "1")
    reset_all_settings()
    try:
        reports = _Reports()
        worker, conn = _queued_worker(worker_pools.CustomRQSimpleWorker, reports)
        monkeypatch.setattr(type(worker), "_start_scheduler", lambda self, *args, **kwargs: None)
        jobs = [Queue("q", connection=conn).enqueue(_hold_the_python_loop, 3.0) for _ in range(2)]

        with caplog.at_level("ERROR", logger="tai42_backend_rq.worker_pools"):
            worker.work(max_jobs=len(jobs))
    finally:
        monkeypatch.delenv("RQ_TASK_TIMEOUT", raising=False)
        reset_all_settings()

    assert [job.get_status(refresh=True).value for job in jobs] == ["failed", "failed"]
    results = [job.latest_result() for job in jobs]
    assert all(result is not None and "JobTimeoutException" in (result.exc_string or "") for result in results)
    assert [record.getMessage() for record in caplog.records if record.name == "tai42_backend_rq.worker_pools"] == []


def test_a_refresh_tick_after_rq_ends_the_execution_refreshes_nothing(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """rq ends a job's execution inside ``perform_job``; a refresher tick between that and the job's end skips."""
    reports = _Reports()
    worker, conn = _queued_worker(worker_pools.CustomRQSimpleWorker, reports)
    worker.job_monitoring_interval = 0
    monkeypatch.setattr(worker_pools, "_REFRESH_TICK_SECONDS", 0.01)
    monkeypatch.setattr(type(worker), "_start_scheduler", lambda self, *args, **kwargs: None)
    end_execution = worker_pools.CustomRQSimpleWorker.cleanup_execution

    def _end_then_linger(self: Any, job: Any, pipeline: Any) -> None:
        end_execution(self, job, pipeline)
        time.sleep(0.1)

    monkeypatch.setattr(worker_pools.CustomRQSimpleWorker, "cleanup_execution", _end_then_linger)
    queue = Queue("q", connection=conn)
    job = queue.enqueue(_hold_the_python_loop, 0.05)

    with caplog.at_level("ERROR", logger="tai42_backend_rq.worker_pools"):
        worker.work(max_jobs=1)

    assert job.get_status(refresh=True).value == "finished"
    assert [record.getMessage() for record in caplog.records if record.name == "tai42_backend_rq.worker_pools"] == []
    assert queue.started_job_registry.get_job_ids() == [], "a refresh put the ended execution back"


def _queued_worker(cls: type[Any], reporter: Any) -> tuple[Any, Any]:
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    queue = Queue("q", connection=conn)
    return cls([queue], connection=conn, report_progress=reporter), conn


def _refuse(*, next_within: float) -> None:
    raise RuntimeError("liveness file /x is owned by live process 1")


def test_a_failed_report_in_the_work_loop_fails_the_worker_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, _ = _queued_worker(worker_pools.CustomRQSimpleWorker, _refuse)
    monkeypatch.setattr(type(worker), "_start_scheduler", lambda self, *args, **kwargs: None)
    runtime = worker_module.RqWorkerRuntime({"burst": True, "loglevel": "info"})
    runtime._worker = worker

    with pytest.raises(RuntimeError, match="stopped because reporting its progress failed") as caught:
        runtime.run_blocking()

    assert str(caught.value.__cause__) == "liveness file /x is owned by live process 1"


def test_a_failed_report_stops_the_work_loop_before_its_next_job(monkeypatch: pytest.MonkeyPatch) -> None:
    worker, _ = _queued_worker(worker_pools.CustomRQSimpleWorker, _refuse)
    polls: list[int] = []
    monkeypatch.setattr(
        worker_pools.SimpleWorker,
        "dequeue_job_and_maintain_ttl",
        lambda self, timeout, max_idle_time=None: polls.append(1),
    )
    worker.heartbeat()

    with pytest.raises(worker_pools.StopRequested):
        worker.dequeue_job_and_maintain_ttl(None)

    assert polls == []


def test_a_failed_report_in_the_horse_monitor_keeps_monitoring_the_horse(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    worker = _bare(worker_pools.CustomRQWorker, _Reports())
    worker.name = "w1"
    worker._report_progress = _refuse
    worker.job_monitoring_interval = 30
    worker._horse_pid = 424242
    worker._killed_horse_pid = 0
    results = iter([(0, 0, None), (0, 0, None), (424242, 0, None)])
    monkeypatch.setattr(worker_pools, "_HORSE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(worker_pools.os, "wait4", lambda pid, flags: next(results))

    with caplog.at_level("ERROR", logger="tai42_backend_rq.worker_pools"):
        assert worker.wait_for_horse() == (424242, 0, None)

    assert len(caplog.records) == 3
    with pytest.raises(RuntimeError, match="stopped because reporting its progress failed"):
        worker.raise_for_liveness_failure()


def test_the_standalone_entry_fails_when_its_liveness_file_is_refused(
    monkeypatch: pytest.MonkeyPatch, liveness_path: Path
) -> None:
    owner = os.getppid()
    liveness_path.write_text(f"{owner}\n5\n")
    conn = fakeredis.FakeStrictRedis(server=fakeredis.FakeServer())
    monkeypatch.setattr(worker_module, "install_fork_hooks", lambda: None)
    monkeypatch.setattr(worker_module, "rq_redis_connection", lambda redis_url: conn)
    monkeypatch.setattr(worker_pools.CustomRQSimpleWorker, "_start_scheduler", lambda self, *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="stopped because reporting its progress failed"):
        worker_module.start_rq_worker(None, None, "info", True, 500, "solo", None)

    assert liveness_path.read_text() == f"{owner}\n5\n"
