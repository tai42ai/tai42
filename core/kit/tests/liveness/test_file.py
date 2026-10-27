"""``LivenessFile`` — the writer side: atomic reports of a promised next pass, owned by one process."""

from __future__ import annotations

import contextlib
import logging
import multiprocessing
import os
import subprocess
import sys
import threading
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Barrier
from pathlib import Path

import pytest

from tai42_kit.liveness import BackendLivenessSettings, LivenessFile, check_liveness
from tai42_kit.liveness import file as file_module


class _Clock:
    """A settable monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _dead_pid() -> int:
    """The pid of a child that has exited and been reaped."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_first_report_writes_pid_and_horizon_atomically(tmp_path: Path) -> None:
    path = tmp_path / "liveness"

    LivenessFile(path, interval=5.0).report(next_within=0.5)

    assert path.read_text() == f"{os.getpid()}\n5.5\n"
    assert [entry.name for entry in tmp_path.iterdir()] == ["liveness"]


def test_reports_inside_the_written_promise_do_not_touch_the_file(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    clock = _Clock()
    writer = LivenessFile(path, interval=5.0, clock=clock)
    writer.report(next_within=0.5)
    os.utime(path, (1.0, 1.0))

    clock.now += 4.9
    writer.report(next_within=0.5)

    assert path.stat().st_mtime == 1.0


def test_a_report_after_the_interval_refreshes_the_file(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    clock = _Clock()
    writer = LivenessFile(path, interval=5.0, clock=clock)
    writer.report(next_within=0.5)
    os.utime(path, (1.0, 1.0))

    clock.now += 5.1
    writer.report(next_within=0.5)

    assert path.stat().st_mtime > 1.0


def test_a_report_promising_a_later_pass_than_the_file_is_written_at_once(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    clock = _Clock()
    writer = LivenessFile(path, interval=5.0, clock=clock)
    writer.report(next_within=2.0)

    clock.now += 0.1
    writer.report(next_within=300.0)

    assert path.read_text() == f"{os.getpid()}\n305\n"


@pytest.mark.parametrize("next_within", [0.0, -1.0])
def test_a_non_positive_promise_is_refused(tmp_path: Path, next_within: float) -> None:
    with pytest.raises(ValueError, match="next_within must be positive"):
        LivenessFile(tmp_path / "liveness", interval=5.0).report(next_within=next_within)


def test_an_unwritable_directory_raises_on_the_first_report(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="missing-dir"):
        LivenessFile(tmp_path / "missing-dir" / "liveness", interval=5.0).report(next_within=1.0)


def test_a_file_owned_by_a_live_process_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    owner = os.getppid()
    path.write_text(f"{owner}\n5\n")

    with pytest.raises(RuntimeError) as caught:
        LivenessFile(path, interval=5.0).report(next_within=1.0)

    assert str(caught.value) == (
        f"liveness file {path} is owned by live process {owner}; "
        "set TAI_BACKEND_LIVENESS_PATH to a distinct path per backend process"
    )
    assert path.read_text() == f"{owner}\n5\n"


def test_a_file_left_by_a_dead_process_is_taken_over(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    path.write_text(f"{_dead_pid()}\n5\n")

    LivenessFile(path, interval=5.0).report(next_within=1.0)

    assert path.read_text() == f"{os.getpid()}\n6\n"


def test_a_file_with_unparsable_content_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    path.write_text("not a pid\n5\n")

    with pytest.raises(ValueError, match=f"liveness file {path} does not hold a pid and a horizon"):
        LivenessFile(path, interval=5.0).report(next_within=1.0)


def test_remove_is_a_noop_when_the_file_is_missing(tmp_path: Path) -> None:
    LivenessFile(tmp_path / "liveness", interval=5.0).remove()


def test_remove_is_a_noop_when_the_directory_is_missing(tmp_path: Path) -> None:
    LivenessFile(tmp_path / "missing-dir" / "liveness", interval=5.0).remove()


def test_remove_unlinks_the_own_file(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    writer = LivenessFile(path, interval=5.0)
    writer.report(next_within=1.0)

    writer.remove()

    assert not path.exists()


def test_remove_removes_a_file_left_by_a_dead_process(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    path.write_text(f"{_dead_pid()}\n5\n")

    LivenessFile(path, interval=5.0).remove()

    assert not path.exists()


def test_a_report_after_remove_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    clock = _Clock()
    writer = LivenessFile(path, interval=5.0, clock=clock)
    writer.report(next_within=1.0)
    writer.remove()

    clock.now += 60.0
    with pytest.raises(RuntimeError) as caught:
        writer.report(next_within=1.0)

    assert str(caught.value) == f"liveness file {path} was removed when this process released it; it reports no more"
    assert not path.exists()
    assert check_liveness(path, grace=25.0).state == "missing"


def test_a_report_after_remove_is_refused_inside_the_written_promise(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    writer = LivenessFile(path, interval=5.0, clock=_Clock())
    writer.report(next_within=1.0)
    writer.remove()

    with pytest.raises(RuntimeError, match="was removed when this process released it"):
        writer.report(next_within=1.0)


def test_remove_leaves_a_foreign_file_and_warns(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "liveness"
    owner = os.getppid()
    path.write_text(f"{owner}\n5\n")

    with caplog.at_level(logging.WARNING, logger="tai42_kit.liveness.file"):
        LivenessFile(path, interval=5.0).remove()

    assert path.read_text() == f"{owner}\n5\n"
    assert [record.getMessage() for record in caplog.records] == [
        f"liveness file {path} is owned by process {owner}, not this one; leaving it in place"
    ]


def test_concurrent_reports_leave_one_coherent_file(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    writer = LivenessFile(path, interval=1e-9)
    start = threading.Barrier(8)
    errors: list[BaseException] = []

    def _report() -> None:
        start.wait()
        try:
            for _ in range(50):
                writer.report(next_within=1e-9)
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_report) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert path.read_text().startswith(f"{os.getpid()}\n")
    assert [entry.name for entry in tmp_path.iterdir()] == ["liveness"]


def test_from_settings_uses_the_path_and_interval(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    writer = LivenessFile.from_settings(BackendLivenessSettings(path=path, interval=2.0, grace=10.0))

    assert writer.path == path
    assert writer.interval == 2.0


def _claim_in_child(path: Path, at_check: Barrier, done: Barrier, outcomes: Queue[str]) -> None:
    """Report once into ``path``, meeting the other claimant right after reading the file's owner."""
    read_owner = file_module._owner

    def owner_then_meet(target: Path) -> int | None:
        owner = read_owner(target)
        # Under the claim lock the other claimant cannot arrive; the meeting then breaks at its
        # timeout and this claim goes on.
        with contextlib.suppress(threading.BrokenBarrierError):
            at_check.wait(timeout=2.0)
        return owner

    file_module._owner = owner_then_meet
    try:
        LivenessFile(path, interval=5.0).report(next_within=1.0)
    except RuntimeError as exc:
        outcomes.put(f"refused: {exc}")
    else:
        outcomes.put("claimed")
    # Both claimants stay alive until each has decided, so each sees the other as a live owner.
    done.wait(timeout=10.0)


@pytest.mark.parametrize("left_by", ["nobody", "a dead process"])
def test_two_processes_claiming_one_file_at_once_leave_one_owner(tmp_path: Path, left_by: str) -> None:
    path = tmp_path / "liveness"
    if left_by == "a dead process":
        path.write_text(f"{_dead_pid()}\n5\n")
    context = multiprocessing.get_context("fork")
    at_check, done, outcomes = context.Barrier(2), context.Barrier(2), context.Queue()
    children = [context.Process(target=_claim_in_child, args=(path, at_check, done, outcomes)) for _ in range(2)]
    for child in children:
        child.start()
    results = sorted(outcomes.get(timeout=10.0) for _ in children)
    for child in children:
        child.join(timeout=10.0)

    assert results[0] == "claimed", results
    assert results[1].startswith(f"refused: liveness file {path} is owned by live process "), results
