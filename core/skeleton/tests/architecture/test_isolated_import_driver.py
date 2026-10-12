"""The isolation driver reports every module's failure, however long its error is."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_DRIVER = Path(__file__).with_name("isolated_import_driver.py")

# Larger than a Linux pipe buffer (64 KiB), so the child cannot hand the whole report over in one write.
_OVERSIZED_MESSAGE_LENGTH = 200_000


def _run_driver(src: Path, package: str) -> dict[str, object]:
    result = subprocess.run(
        [sys.executable, "-I", str(_DRIVER), "--package", package, "--path", str(src)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_a_module_whose_import_error_exceeds_the_pipe_buffer_is_reported_whole(tmp_path: Path) -> None:
    package = tmp_path / "isolation_probe_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "fine.py").write_text("VALUE = 1\n")
    (package / "loud.py").write_text(f"raise RuntimeError('x' * {_OVERSIZED_MESSAGE_LENGTH})\n")
    for index in range(40):
        (package / f"loud_{index}.py").write_text(f"raise RuntimeError('y' * {_OVERSIZED_MESSAGE_LENGTH})\n")

    report = _run_driver(tmp_path, "isolation_probe_pkg")

    failures = report["failures"]
    assert isinstance(failures, dict)
    assert report["checked"] == 43
    assert sorted(failures) == sorted(
        ["isolation_probe_pkg.loud", *(f"isolation_probe_pkg.loud_{index}" for index in range(40))]
    )
    assert "x" * _OVERSIZED_MESSAGE_LENGTH in failures["isolation_probe_pkg.loud"]
    assert "y" * _OVERSIZED_MESSAGE_LENGTH in failures["isolation_probe_pkg.loud_39"]


def test_a_package_whose_modules_all_import_reports_no_failure(tmp_path: Path) -> None:
    package = tmp_path / "isolation_clean_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("from isolation_clean_pkg.leaf import VALUE\n")
    (package / "leaf.py").write_text("VALUE = 1\n")

    report = _run_driver(tmp_path, "isolation_clean_pkg")

    assert report == {"checked": 2, "registering": 0, "failures": {}}


def test_a_module_whose_import_never_finishes_fails_named_once_its_bound_runs_out(tmp_path: Path) -> None:
    package = tmp_path / "isolation_hang_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "fine.py").write_text("VALUE = 1\n")
    (package / "hangs.py").write_text("import time\n\ntime.sleep(10**6)\n")

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(_DRIVER),
            "--package",
            "isolation_hang_pkg",
            "--path",
            str(tmp_path),
            "--import-timeout",
            "10",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["checked"] == 3
    assert report["failures"] == {
        "isolation_hang_pkg.hangs": "the import of isolation_hang_pkg.hangs did not finish within 10 s"
    }


def _run_driver_bounded(src: Path, package: str, import_timeout: int) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(_DRIVER),
            "--package",
            package,
            "--path",
            str(src),
            "--import-timeout",
            str(import_timeout),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def test_a_hanging_module_that_fifty_modules_import_costs_two_bounds_and_is_named_once(tmp_path: Path) -> None:
    package = tmp_path / "isolation_shared_hang_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "base.py").write_text("import time\n\ntime.sleep(10**6)\n")
    importers = [f"m{index:02d}" for index in range(1, 51)]
    for importer in importers:
        (package / f"{importer}.py").write_text("from isolation_shared_hang_pkg import base\n")

    result = _run_driver_bounded(tmp_path, "isolation_shared_hang_pkg", 10)

    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    failures = report["failures"]
    assert report["checked"] == 52
    assert failures["isolation_shared_hang_pkg.base"] == (
        "the import of isolation_shared_hang_pkg.base did not finish within 10 s"
    )
    for importer in importers:
        assert failures[f"isolation_shared_hang_pkg.{importer}"] == (
            f"isolation_shared_hang_pkg.{importer} imports isolation_shared_hang_pkg.base, "
            "whose import did not finish within 10 s"
        )
    assert len(failures) == 51


def test_a_hanging_import_that_catches_exception_is_still_cut_off_and_named(tmp_path: Path) -> None:
    package = tmp_path / "isolation_catching_hang_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "fine.py").write_text("VALUE = 1\n")
    (package / "hangs.py").write_text(
        "import time\n\nwhile True:\n    try:\n        time.sleep(10**6)\n    except Exception:\n        pass\n"
    )

    result = _run_driver_bounded(tmp_path, "isolation_catching_hang_pkg", 10)

    assert result.returncode == 0, result.stderr
    expected = "the import of isolation_catching_hang_pkg.hangs did not finish within 10 s"
    assert json.loads(result.stdout)["failures"] == {"isolation_catching_hang_pkg.hangs": expected}


def test_a_hanging_module_outside_the_package_is_named_and_refused_to_its_importers(tmp_path: Path) -> None:
    (tmp_path / "isolation_outside_hang.py").write_text("import time\n\ntime.sleep(10**6)\n")
    package = tmp_path / "isolation_dephang_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    importers = [f"m{index:02d}" for index in range(1, 21)]
    for importer in importers:
        (package / f"{importer}.py").write_text("import isolation_outside_hang\n")

    result = _run_driver_bounded(tmp_path, "isolation_dephang_pkg", 5)

    assert result.returncode == 0, result.stderr
    failures = json.loads(result.stdout)["failures"]
    refused = "imports isolation_outside_hang, whose import did not finish within 5 s"
    assert failures == {
        "isolation_outside_hang": "the import of isolation_outside_hang did not finish within 5 s",
        **{f"isolation_dephang_pkg.{name}": f"isolation_dephang_pkg.{name} {refused}" for name in importers},
    }


def test_an_importer_that_catches_the_refused_import_still_fails_as_importing_it(tmp_path: Path) -> None:
    package = tmp_path / "isolation_catchimp_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "base.py").write_text("import time\n\ntime.sleep(10**6)\n")
    (package / "plain.py").write_text("from isolation_catchimp_pkg import base\n")
    (package / "optional.py").write_text(
        "try:\n    from isolation_catchimp_pkg import base\nexcept ImportError:\n    base = None\n"
    )

    result = _run_driver_bounded(tmp_path, "isolation_catchimp_pkg", 5)

    assert result.returncode == 0, result.stderr
    refused = "imports isolation_catchimp_pkg.base, whose import did not finish within 5 s"
    assert json.loads(result.stdout)["failures"] == {
        "isolation_catchimp_pkg.base": "the import of isolation_catchimp_pkg.base did not finish within 5 s",
        "isolation_catchimp_pkg.plain": f"isolation_catchimp_pkg.plain {refused}",
        "isolation_catchimp_pkg.optional": f"isolation_catchimp_pkg.optional {refused}",
    }


def test_a_dependency_that_catches_the_refused_import_is_not_warmed_and_its_importer_fails(tmp_path: Path) -> None:
    (tmp_path / "isolation_reg_outhang.py").write_text("import time\n\ntime.sleep(10**6)\n")
    (tmp_path / "isolation_reg_wrapdep.py").write_text(
        "try:\n    import isolation_reg_outhang\nexcept Exception:\n    isolation_reg_outhang = None\n"
    )
    package = tmp_path / "isolation_reg_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "fine.py").write_text("X = 1\n")
    (package / "m.py").write_text("import isolation_reg_wrapdep\n")

    result = _run_driver_bounded(tmp_path, "isolation_reg_pkg", 5)

    assert result.returncode == 0, result.stderr
    refused = "imports isolation_reg_outhang, whose import did not finish within 5 s"
    assert json.loads(result.stdout)["failures"] == {
        "isolation_reg_outhang": "the import of isolation_reg_outhang did not finish within 5 s",
        "isolation_reg_pkg.m": f"isolation_reg_pkg.m {refused}",
    }


def test_a_stand_in_binding_that_outlives_its_bound_fails_naming_the_binding(tmp_path: Path) -> None:
    package = tmp_path / "isolation_bind_bound_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("")

    result = subprocess.run(
        [
            sys.executable,
            "-I",
            str(_DRIVER),
            "--package",
            "isolation_bind_bound_pkg",
            "--path",
            str(tmp_path),
            "--bind-timeout",
            "0.001",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode != 0
    assert "the stand-in binding in the dependency probe did not finish within 0 s" in result.stderr


def test_a_warm_import_that_keeps_finishing_nested_modules_is_not_cut_off(tmp_path: Path) -> None:
    (tmp_path / "isolation_fk_b.py").write_text("import time\n\ntime.sleep(3.5)\n")
    (tmp_path / "isolation_fk_a.py").write_text("import time\n\nimport isolation_fk_b\n\ntime.sleep(3.5)\n")
    package = tmp_path / "isolation_fk_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("import isolation_fk_a\n")

    result = _run_driver_bounded(tmp_path, "isolation_fk_pkg", 5)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"checked": 1, "registering": 0, "failures": {}}


def test_a_warm_import_that_starts_and_finishes_no_module_for_the_bound_ends_the_run_naming_it(
    tmp_path: Path,
) -> None:
    # The module hangs only outside the dependency probe, whose import statements run through its
    # recording wrapper, so the probe warms it and the hang is met in the forking process.
    (tmp_path / "isolation_wd_hang.py").write_text(
        "import builtins\nimport time\n\nif builtins.__import__.__name__ != 'recording_import':\n"
        "    time.sleep(10**6)\n"
    )
    package = tmp_path / "isolation_wd_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("import isolation_wd_hang\n")

    result = _run_driver_bounded(tmp_path, "isolation_wd_pkg", 5)

    assert result.returncode == 1
    assert (
        "the warm-up import of isolation_wd_hang started or finished no module for 5 s "
        "(innermost running: isolation_wd_hang)"
    ) in result.stderr
