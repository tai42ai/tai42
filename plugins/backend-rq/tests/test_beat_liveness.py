"""How the rq beat signals it is alive.

The beat is a loop process: each pass of rq-scheduler's run loop reports, promising the next
pass within the scheduler's interval.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from rq_scheduler.scheduler import Scheduler
from tai42_contract.backend.runtime import ProgressLiveness

from tai42_backend_rq import beat
from tai42_backend_rq.backend import RqBackend, RqBeatRuntime

# The registration decorator's static type is a union (decorator-or-class);
# at runtime the stub app returns the class unchanged.
_RqBackendCls: Any = RqBackend


class _Reports:
    """A progress reporter that records every promise, in order with the calls it is told of."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.promises: list[float] = []

    def __call__(self, *, next_within: float) -> None:
        self.promises.append(next_within)
        self.events.append("report")


@pytest.fixture
def heartbeats(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """rq-scheduler's own heartbeat, recorded instead of refreshing a Redis key."""
    events: list[str] = []
    monkeypatch.setattr(Scheduler, "heartbeat", lambda self: events.append("vendor heartbeat"))
    return events


def _scheduler(interval: float, report: _Reports) -> beat.ProgressReportingScheduler:
    connection: Any = object()
    return beat.ProgressReportingScheduler(connection=connection, interval=interval, report_progress=report)


def test_the_beat_declares_progress_liveness() -> None:
    assert RqBeatRuntime.liveness == ProgressLiveness()


def test_each_heartbeat_reports_the_interval_then_runs_the_vendor_heartbeat(heartbeats: list[str]) -> None:
    report = _Reports(heartbeats)
    scheduler = _scheduler(7.5, report)

    scheduler.heartbeat()
    scheduler.heartbeat()

    assert report.promises == [7.5, 7.5]
    assert heartbeats == ["report", "vendor heartbeat", "report", "vendor heartbeat"]


@pytest.mark.parametrize("interval", [0.0, -3.0])
def test_a_scheduler_that_never_sleeps_promises_the_spin_bound(heartbeats: list[str], interval: float) -> None:
    report = _Reports(heartbeats)

    _scheduler(interval, report).heartbeat()

    assert report.promises == [beat._SPIN_PROMISE_SECONDS]


def test_run_beat_runs_the_reporting_scheduler_with_its_reporter(
    heartbeats: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[beat.ProgressReportingScheduler] = []

    def _one_pass(self: Scheduler, burst: bool = False) -> None:
        assert isinstance(self, beat.ProgressReportingScheduler)
        ran.append(self)
        self.heartbeat()

    monkeypatch.setattr(Scheduler, "run", _one_pass)
    monkeypatch.setattr(beat, "setup_loghandlers", lambda level: None)
    report = _Reports(heartbeats)

    beat.run_beat(["-i", "12"], report_progress=report)

    assert len(ran) == 1
    assert report.promises == [12.0]


async def test_launch_beat_reports_into_the_liveness_file_and_removes_it(
    heartbeats: list[str], monkeypatch: pytest.MonkeyPatch, liveness_path: Path
) -> None:
    seen: list[str] = []

    def _one_pass(self: Scheduler, burst: bool = False) -> None:
        self.heartbeat()
        seen.append(liveness_path.read_text())

    monkeypatch.setattr(Scheduler, "run", _one_pass)
    monkeypatch.setattr(beat, "setup_loghandlers", lambda level: None)

    await _RqBackendCls().launch(["beat", "-i", "20"])

    assert seen == [f"{os.getpid()}\n25\n"]
    assert heartbeats == ["vendor heartbeat"]
    assert not liveness_path.exists()
