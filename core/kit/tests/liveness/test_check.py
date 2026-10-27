"""``check_liveness`` — the reader side: one open file decides alive, stale or missing."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tai42_kit.liveness import LivenessCheck, check_liveness


def _report_at(path: Path, mtime: float, *, pid: int = 4211, horizon: float = 5.5) -> None:
    path.write_text(f"{pid}\n{horizon:g}\n")
    os.utime(path, (mtime, mtime))


def test_a_missing_file_is_not_alive(tmp_path: Path) -> None:
    path = tmp_path / "liveness"

    result = check_liveness(path, grace=25.0)

    assert result == LivenessCheck(state="missing", path=path, pid=None, age=None, horizon=None, grace=25.0)
    assert not result.alive


def test_a_file_inside_its_promise_is_alive_with_its_pid_age_and_horizon(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    _report_at(path, 1000.0)

    result = check_liveness(path, grace=25.0, now=lambda: 1002.3)

    assert result.state == "alive"
    assert result.alive
    assert result.pid == 4211
    assert result.horizon == 5.5
    assert result.age == pytest.approx(2.3)


def test_a_file_at_horizon_plus_grace_is_still_alive(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    _report_at(path, 1000.0, horizon=5.0)

    assert check_liveness(path, grace=25.0, now=lambda: 1030.0).alive


def test_a_file_past_horizon_plus_grace_is_stale(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    _report_at(path, 1000.0, horizon=5.0)

    result = check_liveness(path, grace=25.0, now=lambda: 1030.5)

    assert result.state == "stale"
    assert not result.alive
    assert result.pid == 4211


def test_a_long_promise_keeps_a_slow_loop_alive(tmp_path: Path) -> None:
    path = tmp_path / "liveness"
    _report_at(path, 1000.0, horizon=65.0)

    assert check_liveness(path, grace=25.0, now=lambda: 1080.0).alive


@pytest.mark.parametrize("content", ["4211\n", "garbage\n5\n", "4211\nsoon\n", "4211\n0\n", "4211\n-3\n", "0\n5\n"])
def test_malformed_content_raises(tmp_path: Path, content: str) -> None:
    path = tmp_path / "liveness"
    path.write_text(content)

    with pytest.raises(ValueError, match=f"liveness file {path} does not hold a pid and a horizon"):
        check_liveness(path, grace=25.0)


def test_describe_alive() -> None:
    result = LivenessCheck(
        state="alive", path=Path("/tmp/tai-backend-liveness"), pid=4211, age=2.3, horizon=5.5, grace=25.0
    )

    assert result.describe() == (
        "alive: pid 4211 reported 2.3s ago, next pass promised within 5.5s (+25s grace; /tmp/tai-backend-liveness)"
    )


def test_describe_missing() -> None:
    result = LivenessCheck(
        state="missing", path=Path("/tmp/tai-backend-liveness"), pid=None, age=None, horizon=None, grace=25.0
    )

    assert result.describe() == (
        "not alive: no liveness file at /tmp/tai-backend-liveness "
        "(the process has not reached its loop yet, or it exited)"
    )


def test_describe_stale() -> None:
    result = LivenessCheck(
        state="stale", path=Path("/tmp/tai-backend-liveness"), pid=4211, age=47.0, horizon=5.5, grace=25.0
    )

    assert result.describe() == (
        "not alive: last report 47.0s ago, next pass was promised within 5.5s (+25s grace) "
        "(pid 4211, /tmp/tai-backend-liveness)"
    )


def test_describe_keeps_a_fractional_grace() -> None:
    result = LivenessCheck(state="stale", path=Path("/tmp/liveness"), pid=7, age=3.6, horizon=1.25, grace=2.5)

    assert result.describe() == (
        "not alive: last report 3.6s ago, next pass was promised within 1.25s (+2.5s grace) (pid 7, /tmp/liveness)"
    )
