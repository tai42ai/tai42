"""A worker's embedded beat (``-B``): its own liveness file, read by the worker's tick.

celery forks the embedded beat from the worker; the beat reports each scheduler tick into
``<TAI_BACKEND_LIVENESS_PATH>-beat`` with its own pid, the worker withholds its own report while
that promise is overdue, and the worker removes the beat's file once the beat has ended.
"""

from __future__ import annotations

import multiprocessing
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import fakeredis
import pytest
from celery.signals import beat_init
from redbeat import RedBeatScheduler
from tai42_kit.liveness import BackendLivenessSettings, backend_liveness_settings

from tai42_backend_celery.core import beat_scheduler, embedded_beat
from tai42_backend_celery.core.app import celery_app


class _Beat:
    """An embedded beat that is running or has ended, and that ends, or does not, when asked to."""

    def __init__(self, *, running: bool = True, ends: bool = True, pid: int | None = 4242, name: str = "Beat") -> None:
        self.running = running
        self.ends = ends
        self.pid = pid
        self.name = name
        self.asked: list[str] = []
        self.joins: list[float] = []
        if pid is None:
            self.service = SimpleNamespace(stop=lambda: self._asked("service.stop"))
        else:
            self.terminate = lambda: self._asked("terminate")

    def _asked(self, how: str) -> None:
        self.asked.append(how)
        if self.ends:
            self.running = False

    def join(self, timeout: float) -> None:
        self.joins.append(timeout)

    def is_alive(self) -> bool:
        return self.running


def _beat_path(liveness_path: Path) -> Path:
    return liveness_path.with_name(liveness_path.name + "-beat")


def _tick_in_child() -> None:
    """One tick of the reporting scheduler, with RedBeat's own tick returning a 3s sleep."""
    RedBeatScheduler.tick = lambda self, *args, **kwargs: 3.0  # type: ignore[method-assign]
    scheduler: Any = object.__new__(beat_scheduler.ProgressReportingScheduler)
    scheduler.tick()


def _forked_beat_tick() -> Any:
    child = multiprocessing.get_context("fork").Process(target=_tick_in_child)
    child.start()
    child.join(10)
    assert child.exitcode == 0
    return child


def test_the_beat_file_sits_next_to_the_worker_s() -> None:
    settings = BackendLivenessSettings(path=Path("/run/tai/liveness"))

    assert embedded_beat.embedded_beat_path(settings) == Path("/run/tai/liveness-beat")


def test_register_hands_the_beat_scheduler_a_reporter_that_writes_the_beat_file(liveness_path: Path) -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())

    child = _forked_beat_tick()

    assert child.pid != os.getpid()
    assert _beat_path(liveness_path).read_text() == f"{child.pid}\n8\n"
    assert not liveness_path.exists()


def test_a_second_register_keeps_the_one_beat_reporter() -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    first = beat_scheduler._report

    embedded_beat.register(celery_app, backend_liveness_settings())

    assert beat_scheduler._report == first


def test_with_no_embedded_beat_the_check_reads_nothing(liveness_path: Path) -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    embedded_beat.watch(None)

    embedded_beat.check()

    assert not _beat_path(liveness_path).exists()


def test_an_embedded_beat_that_keeps_its_promise_passes_the_check() -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    _forked_beat_tick()
    embedded_beat.watch(_Beat())

    embedded_beat.check()


class _LockWaitingBeat:
    """A ``Service``-shaped beat whose RedBeat lock another beat holds."""

    def __init__(self) -> None:
        self.scheduler = SimpleNamespace(
            app=celery_app, lock_key="redbeat::lock", lock_timeout=300, max_interval=60, lock=None
        )
        self._is_shutdown = threading.Event()


def _wait_for_a_held_lock_in_child() -> None:
    """Start a beat that waits for the lock another beat holds, until it is ended."""
    client = fakeredis.FakeRedis()
    client.set("redbeat::lock", "another beat")
    beat_scheduler.get_redis = lambda app: client  # type: ignore[assignment]
    beat_init.send(sender=_LockWaitingBeat())


def test_an_embedded_beat_waiting_for_the_lock_passes_the_check(liveness_path: Path) -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    child = multiprocessing.get_context("fork").Process(target=_wait_for_a_held_lock_in_child, daemon=True)
    child.start()
    try:
        deadline = time.monotonic() + 10.0
        while not _beat_path(liveness_path).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        embedded_beat.watch(_Beat(pid=child.pid))

        embedded_beat.check()

        assert _beat_path(liveness_path).read_text().startswith(f"{child.pid}\n")
    finally:
        child.terminate()
        child.join(10)


def test_an_embedded_beat_past_its_promise_fails_the_check(liveness_path: Path) -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    child = _forked_beat_tick()
    past = time.time() - 60
    os.utime(_beat_path(liveness_path), (past, past))
    embedded_beat.watch(_Beat())

    with pytest.raises(RuntimeError) as raised:
        embedded_beat.check()

    message = str(raised.value)
    assert message.startswith("the embedded beat (-B) is not alive: last report 60.")
    assert f"(pid {child.pid}, {_beat_path(liveness_path)})" in message
    assert message.endswith("; this worker reports nothing until its beat ticks again")


def test_an_embedded_beat_that_never_ticked_fails_the_check(liveness_path: Path) -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    embedded_beat.watch(_Beat())

    with pytest.raises(RuntimeError, match=r"^the embedded beat \(-B\) is not alive: no liveness file at "):
        embedded_beat.check()


def test_release_joins_the_ended_beat_then_removes_its_file(liveness_path: Path) -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    child = _forked_beat_tick()
    embedded_beat.watch(child)

    embedded_beat.release()

    assert not _beat_path(liveness_path).exists()


def test_release_asks_a_forked_beat_still_running_to_end_and_bounds_its_join() -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    beat = _Beat()
    embedded_beat.watch(beat)

    embedded_beat.release()

    assert (beat.asked, beat.joins) == (["terminate"], [10.0])


def test_release_asks_a_threaded_beat_still_running_to_end() -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    beat = _Beat(pid=None)
    embedded_beat.watch(beat)

    embedded_beat.release()

    assert (beat.asked, beat.joins) == (["service.stop"], [10.0])


def test_release_leaves_an_ended_beat_alone() -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    beat = _Beat(running=False)
    embedded_beat.watch(beat)

    embedded_beat.release()

    assert (beat.asked, beat.joins) == ([], [])


def test_release_raises_on_a_beat_that_does_not_end_and_leaves_its_file(liveness_path: Path) -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    _forked_beat_tick()
    embedded_beat.watch(_Beat(ends=False, pid=4242))

    with pytest.raises(RuntimeError) as raised:
        embedded_beat.release()

    assert str(raised.value) == "the embedded beat (-B, pid 4242) did not end within 10s of the worker stopping"
    assert _beat_path(liveness_path).exists()


def test_release_names_a_threaded_beat_by_its_thread() -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    embedded_beat.watch(_Beat(ends=False, pid=None, name="Beat"))

    with pytest.raises(RuntimeError, match=r"^the embedded beat \(-B, thread Beat\) did not end within 10s"):
        embedded_beat.release()


def test_release_with_no_embedded_beat_removes_nothing_and_raises_nothing(liveness_path: Path) -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())
    embedded_beat.watch(None)

    embedded_beat.release()

    assert not _beat_path(liveness_path).exists()


def test_register_adds_the_watch_step_to_the_app_s_worker_blueprint() -> None:
    embedded_beat.register(celery_app, backend_liveness_settings())

    assert embedded_beat.BeatWatchStep in embedded_beat.worker_steps(celery_app)


def test_the_watch_step_runs_after_celery_s_beat_step_and_watches_the_beat_it_built() -> None:
    from celery.worker.components import Beat

    beat = _Beat()

    embedded_beat.BeatWatchStep(SimpleNamespace()).create(SimpleNamespace(beat=beat))

    assert embedded_beat.BeatWatchStep.requires == (Beat,)
    assert embedded_beat._beat is beat
