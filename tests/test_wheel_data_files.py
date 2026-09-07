"""Every git-tracked non-Python file under a workspace member's ``src`` ships in
that member's wheel. A data file the package reads at runtime (a SQL baseline, a
plugin descriptor, ``py.typed``) that setuptools' ``package-data`` does not name
is silently absent from the published artifact while every in-repo run sees it
on disk; building each wheel and diffing it against the tracked tree is the only
check that sees what a fresh install sees."""

from __future__ import annotations

import fnmatch
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _members() -> list[Path]:
    workspace = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]["workspace"]
    excluded = {ROOT / e for e in workspace.get("exclude", [])}
    found: list[Path] = []
    for pattern in workspace["members"]:
        for path in sorted(ROOT.glob(pattern)):
            if path in excluded or not (path / "pyproject.toml").is_file() or not (path / "src").is_dir():
                continue
            found.append(path)
    return found


def _tracked_data_files(member: Path) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", "src"],
        cwd=member,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return sorted(f.removeprefix("src/") for f in out if not f.endswith(".py") and not fnmatch.fnmatch(f, "*/tests/*"))


@pytest.mark.parametrize("member", _members(), ids=lambda p: str(p.relative_to(ROOT)))
def test_wheel_carries_every_tracked_data_file(member: Path, tmp_path: Path):
    expected = _tracked_data_files(member)
    if not expected:
        pytest.skip("no tracked data files under src")
    name = tomllib.loads((member / "pyproject.toml").read_text())["project"]["name"]
    subprocess.run(
        ["uv", "build", "--package", name, "--wheel", "--out-dir", str(tmp_path)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = tmp_path.glob("*.whl")
    shipped = set(zipfile.ZipFile(wheel).namelist())
    missing = [f for f in expected if f not in shipped]
    assert not missing, f"{name}: tracked data files absent from {wheel.name}: {missing}"
