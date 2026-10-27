"""``tai42-backend-probe`` — exit 0 while the liveness file's promise is not overdue past the grace, 1 otherwise."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from tai42_kit.liveness import probe


@pytest.fixture
def report_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "liveness"
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_PATH", str(path))
    return path


def _report(path: Path, age: float, horizon: float = 5.5) -> None:
    path.write_text(f"4211\n{horizon:g}\n")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))


def test_a_file_inside_its_promise_exits_zero(report_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _report(report_path, age=1.0)

    assert probe.main([]) == 0

    out = capsys.readouterr().out
    assert out.startswith("alive: pid 4211 reported ")
    assert out.endswith(f"s ago, next pass promised within 5.5s (+25s grace; {report_path})\n")


def test_a_missing_file_exits_one(report_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert probe.main([]) == 1

    assert capsys.readouterr().out == (
        f"not alive: no liveness file at {report_path} (the process has not reached its loop yet, or it exited)\n"
    )


def test_an_overdue_file_exits_one(report_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _report(report_path, age=60.0)

    assert probe.main([]) == 1

    assert capsys.readouterr().out.startswith("not alive: last report 60.")


def test_the_grace_setting_is_read_from_the_env(
    report_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("TAI_BACKEND_LIVENESS_GRACE", "120")
    _report(report_path, age=60.0)

    assert probe.main([]) == 0
    assert "(+120s grace;" in capsys.readouterr().out


def test_a_dotenv_in_the_working_directory_is_not_read(
    report_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / ".env").write_text("TAI_BACKEND_LIVENESS_GRACE=120\n")
    monkeypatch.chdir(workdir)
    _report(report_path, age=60.0)

    assert probe.main([]) == 1

    assert capsys.readouterr().out.startswith("not alive: last report 60.")


def test_a_dotenv_in_the_working_directory_does_not_move_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "stale-liveness"
    _report(stale, age=1.0)
    (tmp_path / ".env").write_text(f"TAI_BACKEND_LIVENESS_PATH={stale}\n")
    monkeypatch.delenv("TAI_BACKEND_LIVENESS_PATH", raising=False)
    monkeypatch.chdir(tmp_path)

    probe.main([])

    assert str(stale) not in capsys.readouterr().out


def test_the_grace_flag_overrides_the_setting(report_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _report(report_path, age=10.0)

    assert probe.main(["--grace", "2"]) == 1
    assert "(+2s grace)" in capsys.readouterr().out


def test_a_negative_grace_flag_exits_two(report_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _report(report_path, age=0.0)

    with pytest.raises(SystemExit) as exited:
        probe.main(["--grace", "-1"])

    assert exited.value.code == 2
    assert "--grace must be zero or more seconds, got -1" in capsys.readouterr().err


def test_unparsable_file_raises(report_path: Path) -> None:
    report_path.write_text("garbage\n")

    with pytest.raises(ValueError, match="does not hold a pid and a horizon"):
        probe.main([])


def test_console_script_is_declared() -> None:
    (script,) = entry_points(group="console_scripts", name="tai42-backend-probe")

    assert script.value == "tai42_kit.liveness.probe:main"


def test_console_script_runs_and_reports(report_path: Path) -> None:
    executable = shutil.which("tai42-backend-probe")
    assert executable is not None
    _report(report_path, age=0.0)

    result = subprocess.run([executable], capture_output=True, text=True, check=False, env=os.environ.copy())

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("alive: pid 4211 reported ")
