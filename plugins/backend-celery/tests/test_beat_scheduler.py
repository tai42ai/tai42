"""How the celery beat signals it is alive.

The beat is a loop process: its scheduler reports each tick, promising the next within the
interval the beat service will sleep.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest
from redbeat import RedBeatScheduler
from tai42_contract.backend.runtime import ProgressLiveness

import tai42_backend_celery.core.backend as backend_module
from tai42_backend_celery.core import beat_scheduler
from tai42_backend_celery.core.app import celery_app
from tai42_backend_celery.core.backend import CeleryBackend, CeleryBeatRuntime


class _Reports:
    def __init__(self) -> None:
        self.promises: list[float] = []

    def __call__(self, *, next_within: float) -> None:
        self.promises.append(next_within)


def _bare_scheduler() -> beat_scheduler.ProgressReportingScheduler:
    """A scheduler with no Redis behind it: only ``tick`` is driven, with RedBeat's own patched."""
    scheduler: Any = object.__new__(beat_scheduler.ProgressReportingScheduler)
    return scheduler


@pytest.fixture
def redbeat_ticks(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """RedBeat's own tick, returning the next interval from this list in turn."""
    intervals: list[float] = []
    monkeypatch.setattr(RedBeatScheduler, "tick", lambda self, *args, **kwargs: intervals.pop(0))
    return intervals


def test_the_beat_declares_progress_liveness() -> None:
    assert CeleryBeatRuntime.liveness == ProgressLiveness()


def test_the_app_runs_the_reporting_scheduler() -> None:
    assert celery_app.conf.beat_scheduler == "tai42_backend_celery.core.beat_scheduler.ProgressReportingScheduler"


def test_a_tick_reports_the_interval_redbeat_returned_and_returns_it(redbeat_ticks: list[float]) -> None:
    report = _Reports()
    beat_scheduler.register(report)
    redbeat_ticks.extend([2.0, 60.0])
    scheduler = _bare_scheduler()

    assert scheduler.tick() == 2.0
    assert scheduler.tick() == 60.0

    assert report.promises == [2.0, 60.0]


@pytest.mark.parametrize("interval", [0.0, -1.0])
def test_a_tick_that_returns_no_sleep_promises_the_spin_bound(redbeat_ticks: list[float], interval: float) -> None:
    report = _Reports()
    beat_scheduler.register(report)
    redbeat_ticks.append(interval)

    assert _bare_scheduler().tick() == interval

    assert report.promises == [beat_scheduler._SPIN_PROMISE_SECONDS]


def test_a_tick_with_no_reporter_raises(redbeat_ticks: list[float]) -> None:
    redbeat_ticks.append(5.0)

    with pytest.raises(RuntimeError, match="ticked with no progress reporter registered"):
        _bare_scheduler().tick()


def test_a_second_register_with_the_same_reporter_changes_nothing(redbeat_ticks: list[float]) -> None:
    report = _Reports()
    beat_scheduler.register(report)
    beat_scheduler.register(report)
    redbeat_ticks.append(3.0)

    _bare_scheduler().tick()

    assert report.promises == [3.0]


def test_a_second_register_with_another_reporter_raises() -> None:
    beat_scheduler.register(_Reports())

    with pytest.raises(RuntimeError, match="already reports its progress through another reporter"):
        beat_scheduler.register(_Reports())


async def test_launch_beat_registers_its_reporter_and_removes_the_file(
    redbeat_ticks: list[float], monkeypatch: pytest.MonkeyPatch, liveness_path: Path
) -> None:
    seen: list[str] = []
    redbeat_ticks.append(4.0)

    def _beat_cli() -> None:
        _bare_scheduler().tick()
        seen.append(liveness_path.read_text())

    monkeypatch.setattr(backend_module, "_celery_cli_main", _beat_cli)
    monkeypatch.setattr(sys, "argv", list(sys.argv))

    await CeleryBackend().launch(["beat"])

    assert seen == [f"{os.getpid()}\n9\n"]
    assert not liveness_path.exists()
