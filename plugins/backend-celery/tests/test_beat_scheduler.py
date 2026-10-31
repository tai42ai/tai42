"""How the celery beat signals it is alive.

The beat is a loop process: its scheduler reports each tick, promising the next within the
interval the beat service will sleep. Before its first tick it reports each try for RedBeat's
lock, once a second, while another beat holds it.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fakeredis
import pytest
import redbeat.schedulers
from celery.signals import beat_init
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


_LOCK_KEY = "redbeat::lock"


class _LockBeat:
    """A ``Service``-shaped beat: the scheduler RedBeat's lock receiver reads, and the stop event."""

    def __init__(self, *, lock_key: str = _LOCK_KEY) -> None:
        self.scheduler = SimpleNamespace(
            app=celery_app, lock_key=lock_key, lock_timeout=300, max_interval=60, lock=None
        )
        self._is_shutdown = threading.Event()


@pytest.fixture
def lock_redis(monkeypatch: pytest.MonkeyPatch) -> fakeredis.FakeRedis:
    """The Redis RedBeat's lock lives in, for this module's receiver and RedBeat's own."""
    client = fakeredis.FakeRedis()
    monkeypatch.setattr(beat_scheduler, "get_redis", lambda app: client)
    monkeypatch.setattr(redbeat.schedulers, "get_redis", lambda app: client)
    return client


def _start_beat_init(beat: _LockBeat) -> threading.Thread:
    sending = threading.Thread(target=beat_init.send, kwargs={"sender": beat}, daemon=True)
    sending.start()
    return sending


def _wait_for(condition: Any, *, within: float) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_a_beat_waiting_for_redbeat_s_lock_reports_each_second(lock_redis: fakeredis.FakeRedis) -> None:
    report = _Reports()
    beat_scheduler.register(report)
    lock_redis.set(_LOCK_KEY, "another beat")
    beat = _LockBeat()

    sending = _start_beat_init(beat)
    try:
        assert _wait_for(lambda: len(report.promises) >= 2, within=3.0), report.promises
        assert set(report.promises) == {1.0}

        lock_redis.delete(_LOCK_KEY)
        sending.join(timeout=3.0)

        assert not sending.is_alive()
        assert beat.scheduler.lock is not None
        # The lock's token lives with the thread that took it; Redis holds a token of this lock's.
        assert lock_redis.get(_LOCK_KEY) not in (None, b"another beat")
    finally:
        beat._is_shutdown.set()
        lock_redis.delete(_LOCK_KEY)


def test_register_replaces_redbeat_s_lock_receiver() -> None:
    report = _Reports()
    beat_scheduler.register(report)
    beat_scheduler.register(report)

    assert beat_init.receivers is not None
    receivers = [receiver for _, receiver in beat_init.receivers]

    assert redbeat.schedulers.acquire_distributed_beat_lock not in receivers
    assert receivers.count(beat_scheduler._acquire_lock_reporting) == 1


@pytest.fixture
def fast_lock_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(beat_scheduler, "_LOCK_POLL_SECONDS", 0.01)


@pytest.mark.usefixtures("fast_lock_poll")
def test_a_held_lock_is_logged_once_at_warning(
    lock_redis: fakeredis.FakeRedis, caplog: pytest.LogCaptureFixture
) -> None:
    report = _Reports()
    beat_scheduler.register(report)
    lock_redis.set(_LOCK_KEY, "another beat")
    beat = _LockBeat()

    with caplog.at_level(logging.INFO, logger=beat_scheduler.__name__):
        sending = _start_beat_init(beat)
        assert _wait_for(lambda: len(report.promises) >= 5, within=3.0), report.promises
        lock_redis.delete(_LOCK_KEY)
        sending.join(timeout=3.0)

    assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
        (
            logging.WARNING,
            f"beat: RedBeat lock {_LOCK_KEY} is held by another beat; waiting for it and reporting meanwhile",
        ),
        (logging.INFO, "beat: Acquired lock"),
    ]


@pytest.mark.usefixtures("fast_lock_poll")
def test_a_stop_while_waiting_leaves_the_lock_untaken(lock_redis: fakeredis.FakeRedis) -> None:
    report = _Reports()
    beat_scheduler.register(report)
    lock_redis.set(_LOCK_KEY, "another beat")
    beat = _LockBeat()

    sending = _start_beat_init(beat)
    assert _wait_for(lambda: len(report.promises) >= 1, within=3.0)
    beat._is_shutdown.set()
    sending.join(timeout=3.0)

    assert not sending.is_alive()
    assert beat.scheduler.lock is None
    assert lock_redis.get(_LOCK_KEY) == b"another beat"


def test_an_empty_lock_key_takes_no_lock(lock_redis: fakeredis.FakeRedis) -> None:
    report = _Reports()
    beat_scheduler.register(report)
    beat = _LockBeat(lock_key="")

    beat_scheduler._acquire_lock_reporting(sender=beat)

    assert beat.scheduler.lock is None
    assert report.promises == []
    assert lock_redis.keys() == []


def test_the_lock_is_built_with_redbeat_s_timeout_and_extend_script(lock_redis: fakeredis.FakeRedis) -> None:
    beat_scheduler.register(_Reports())
    beat = _LockBeat()

    beat_scheduler._acquire_lock_reporting(sender=beat)

    lock = beat.scheduler.lock
    assert lock.name == _LOCK_KEY
    assert lock.timeout == 300
    assert lock.sleep == 60
    assert lock.lua_extend.script == redbeat.schedulers.LUA_EXTEND_TO_SCRIPT
    ttl_ms = lock_redis.pttl(_LOCK_KEY)
    assert isinstance(ttl_ms, int)
    assert 0 < ttl_ms <= 300_000


def test_a_lock_wait_with_no_reporter_raises(lock_redis: fakeredis.FakeRedis) -> None:
    with pytest.raises(RuntimeError, match="waits for RedBeat's lock with no progress reporter registered"):
        beat_scheduler._acquire_lock_reporting(sender=_LockBeat())

    assert lock_redis.keys() == []
