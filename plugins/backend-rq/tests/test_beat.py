"""The rq beat: rq-scheduler's ``Scheduler`` on a connection the kit seam built.

``run_beat`` parses the scheduler's own options (with rq-scheduler's names, defaults and
meanings), builds the connection the way the rq worker does, so the ``RQ_*`` retry and
socket timeouts reach every scheduler command, and runs the progress-reporting scheduler with
the plugin's queue class.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import click
import pytest
from redis import Redis
from tai42_kit.clients.impl.redis import _LoggedSyncRetry

from tai42_backend_rq import beat
from tai42_backend_rq.queue import TASK_BUDGET_QUEUE_PATH
from tai42_backend_rq.settings import rq_settings


class _FakeScheduler:
    instances: ClassVar[list[_FakeScheduler]] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.ran: dict[str, Any] | None = None
        type(self).instances.append(self)

    def run(self, **kwargs: Any) -> None:
        self.ran = kwargs


@pytest.fixture
def scheduler(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeScheduler]]:
    for key in list(os.environ):
        if key.startswith(("RQ_", "TAI_DEFAULT_")):
            monkeypatch.delenv(key, raising=False)
    _FakeScheduler.instances.clear()
    monkeypatch.setattr(beat, "ProgressReportingScheduler", _FakeScheduler)
    levels: list[str] = []
    monkeypatch.setattr(beat, "setup_loghandlers", levels.append)
    _FakeScheduler.levels = levels  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "path", list(sys.path))
    rq_settings.cache_clear()
    yield _FakeScheduler
    rq_settings.cache_clear()


def _no_report(*, next_within: float) -> None:
    """A reporter for tests that do not look at the beat's progress."""


def _run_beat(args: list[str]) -> None:
    beat.run_beat(args, report_progress=_no_report)


def _ran(scheduler: type[_FakeScheduler]) -> _FakeScheduler:
    [instance] = scheduler.instances
    return instance


def _connection_kwargs(instance: _FakeScheduler) -> dict[str, Any]:
    connection = instance.kwargs["connection"]
    assert isinstance(connection, Redis)
    return connection.connection_pool.connection_kwargs


def test_the_scheduler_runs_on_a_connection_with_the_kit_sync_retry(scheduler, monkeypatch) -> None:
    monkeypatch.setenv("RQ_REDIS_URL", "redis://configured:6390/2")
    monkeypatch.setenv("RQ_RETRY_ATTEMPTS", "2")
    monkeypatch.setenv("RQ_SOCKET_TIMEOUT", "3")

    _run_beat(["-i", "5"])

    instance = _ran(scheduler)
    connection_kwargs = _connection_kwargs(instance)
    assert (connection_kwargs["host"], connection_kwargs["port"], connection_kwargs["db"]) == ("configured", 6390, 2)
    assert connection_kwargs["decode_responses"] is False
    assert connection_kwargs["socket_timeout"] == 3
    retry = connection_kwargs["retry"]
    assert isinstance(retry, _LoggedSyncRetry)
    assert retry._budget == 2
    assert retry._label == "RQ_REDIS_URL"
    assert instance.kwargs["interval"] == 5.0
    assert instance.kwargs["queue_class"] == TASK_BUDGET_QUEUE_PATH
    assert instance.kwargs["job_class"] is None
    assert instance.ran == {"burst": False}
    assert scheduler.levels == ["INFO"]  # type: ignore[attr-defined]


def test_the_defaults_are_the_vendor_defaults(scheduler) -> None:
    _run_beat([])

    instance = _ran(scheduler)
    assert instance.kwargs["interval"] == 60.0
    connection_kwargs = _connection_kwargs(instance)
    assert (connection_kwargs["host"], connection_kwargs["port"], connection_kwargs["db"]) == ("localhost", 6379, 0)
    assert not isinstance(connection_kwargs.get("retry"), _LoggedSyncRetry)


def test_the_url_falls_back_to_the_shared_default(scheduler, monkeypatch) -> None:
    monkeypatch.setenv("TAI_DEFAULT_REDIS_URL", "redis://shared:6391/4")

    _run_beat([])

    connection_kwargs = _connection_kwargs(_ran(scheduler))
    assert (connection_kwargs["host"], connection_kwargs["port"], connection_kwargs["db"]) == ("shared", 6391, 4)


def test_the_url_option_overrides_the_setting(scheduler, monkeypatch) -> None:
    monkeypatch.setenv("RQ_REDIS_URL", "redis://configured:6390/2")
    monkeypatch.setenv("RQ_RETRY_ATTEMPTS", "1")

    _run_beat(["--url", "redis://elsewhere:6392/5"])

    connection_kwargs = _connection_kwargs(_ran(scheduler))
    assert (connection_kwargs["host"], connection_kwargs["port"], connection_kwargs["db"]) == ("elsewhere", 6392, 5)
    assert connection_kwargs["retry"]._label == "RQ_REDIS_URL"


def test_burst_reaches_the_run(scheduler) -> None:
    _run_beat(["-b"])

    assert _ran(scheduler).ran == {"burst": True}


@pytest.mark.parametrize(("flag", "level"), [("-v", "DEBUG"), ("--verbose", "DEBUG"), ("--quiet", "WARNING")])
def test_the_log_level_follows_the_vendor_flags(scheduler, flag: str, level: str) -> None:
    _run_beat([flag])

    assert scheduler.levels == [level]  # type: ignore[attr-defined]


def test_the_job_class_is_handed_through(scheduler) -> None:
    _run_beat(["-j", "rq.job.Job"])

    assert _ran(scheduler).kwargs["job_class"] == "rq.job.Job"


def test_path_and_pid_apply_as_the_vendor_does(scheduler, tmp_path: Path) -> None:
    pid_file = tmp_path / "beat.pid"

    _run_beat(["--path", "/opt/a:/opt/b", "--pid", str(pid_file)])

    assert sys.path[:2] == ["/opt/a", "/opt/b"]
    assert pid_file.read_text() == str(os.getpid())


@pytest.mark.parametrize("args", [["-H", "localhost"], ["--port", "6379"], ["-q", "x.Queue"]])
def test_the_connection_and_queue_class_options_are_refused(scheduler, args: list[str]) -> None:
    with pytest.raises(click.UsageError):
        _run_beat(args)

    assert scheduler.instances == []


def test_the_connection_is_closed_after_the_scheduler_ends(scheduler, monkeypatch) -> None:
    closed: list[bool] = []
    monkeypatch.setattr(Redis, "close", lambda self: closed.append(True))

    _run_beat(["-b"])

    assert closed == [True]
