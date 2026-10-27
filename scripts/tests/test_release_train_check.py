"""Tests for the release-train check: a release branch must release every unit whose ranges it moves.

Each case is a throwaway git repo under ``tmp_path``: ``main`` carries an in-sync workspace, the
release branch bumps versions on top of it, and the check runs over the branch before its ranges
are written.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from _range_sync_support import _write

import release_train_check

_CONNECTOR = "plugins/connector-demo"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60)


def _set_version(root: Path, unit: str, version: str) -> None:
    pyproject = root / unit / "pyproject.toml"
    if pyproject.is_file():
        pyproject.write_text(re.sub(r'(?m)^version = "[^"]*"', f'version = "{version}"', pyproject.read_text()))
        return
    descriptor = root / unit / "tai-plugin.yml"
    descriptor.write_text(re.sub(r"(?m)^version: .*$", f"version: {version}", descriptor.read_text()))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """``main``: contract 2.3.0, kit and a plugin that pin it, a plugin with no first-party deps,
    and a descriptor-only connector; the release branch checked out on top of it."""
    root = tmp_path / "repo"
    _write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["core/*", "plugins/*"]\n')
    _write(
        root / "core/contract/pyproject.toml",
        '[project]\nname = "tai42-contract"\nversion = "2.3.0"\ndependencies = []\n',
    )
    _write(
        root / "core/kit/pyproject.toml",
        '[project]\nname = "tai42-kit"\nversion = "5.1.0"\ndependencies = ["tai42-contract>=2.3,<3"]\n',
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        '[project]\nname = "tai42-demo"\nversion = "1.0.4"\n'
        'dependencies = ["tai42-contract>=2.3,<3", "tai42-kit>=5.1,<6"]\n',
    )
    _write(root / "plugins/demo/tai-plugin.yml", "package: tai42-demo\nversion: 1.0.4\ncontract: '>=2.3,<3'\n")
    _write(
        root / "plugins/standalone/pyproject.toml",
        '[project]\nname = "tai42-standalone"\nversion = "1.0.0"\ndependencies = ["httpx>=0.28"]\n',
    )
    _write(root / _CONNECTOR / "tai-plugin.yml", "version: 1.2.0\ncontract: '>=2.3,<3'\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "alice@example.com")
    _git(root, "config", "user.name", "Alice")
    _git(root, "config", "commit.gpgsign", "false")
    _git(root, "add", "--all")
    _git(root, "commit", "-qm", "base")
    _git(root, "checkout", "-q", "-b", "release")
    return root


def _release(root: Path, versions: dict[str, str]) -> None:
    for unit, version in versions.items():
        _set_version(root, unit, version)
    _git(root, "commit", "-qam", "chore: release main")


def _run(root: Path, capsys) -> tuple[int, str, str]:
    code = release_train_check.main(["--base", "main", "--root", str(root)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_core_major_that_releases_every_dependent_passes(repo: Path, capsys):
    _release(repo, {"core/contract": "3.0.0", "core/kit": "5.1.1", "plugins/demo": "1.0.5", _CONNECTOR: "1.2.1"})
    code, out, _err = _run(repo, capsys)
    assert code == 0
    assert "every unit whose ranges or contract pins move is released" in out


def test_a_core_major_that_leaves_a_dependent_unreleased_fails_naming_it_and_its_moves(repo: Path, capsys):
    _release(repo, {"core/contract": "3.0.0", "core/kit": "5.1.1", _CONNECTOR: "1.2.1"})
    code, _out, err = _run(repo, capsys)
    assert code == 1
    assert (
        "release-train: plugins/demo moves plugins/demo/pyproject.toml: "
        "'tai42-contract>=2.3,<3' -> 'tai42-contract>=3.0,<4' but this release does not release plugins/demo "
        "(its version 1.0.4 equals main's)"
    ) in err
    assert "plugins/demo moves plugins/demo/tai-plugin.yml: '>=2.3,<3' -> '>=3.0,<4'" in err
    assert "core/kit moves" not in err
    assert f"{_CONNECTOR} moves" not in err


def test_a_release_that_moves_no_range_passes(repo: Path, capsys):
    _release(repo, {"core/contract": "2.3.1", "plugins/standalone": "1.0.1"})
    assert release_train_check.uncovered_moves(repo, "main") == {}
    assert _run(repo, capsys)[0] == 0


def test_a_descriptor_only_contract_move_needs_the_component_released(repo: Path, capsys):
    _release(repo, {"core/contract": "2.4.0", "core/kit": "5.1.1", "plugins/demo": "1.0.5"})
    uncovered = release_train_check.uncovered_moves(repo, "main")
    assert list(uncovered) == [_CONNECTOR]
    assert [(change.file, change.old, change.new) for change in uncovered[_CONNECTOR].changes] == [
        (f"{_CONNECTOR}/tai-plugin.yml", ">=2.3,<3", ">=2.4,<3")
    ]
    _release(repo, {_CONNECTOR: "1.2.1"})
    assert release_train_check.uncovered_moves(repo, "main") == {}


def test_a_moved_unit_the_base_does_not_carry_is_not_released(repo: Path, capsys):
    _write(
        repo / "plugins/fresh/pyproject.toml",
        '[project]\nname = "tai42-fresh"\nversion = "0.1.0"\ndependencies = ["tai42-contract>=2.3,<3"]\n',
    )
    _git(repo, "add", "--all")
    _release(repo, {"core/contract": "3.0.0", "core/kit": "5.1.1", "plugins/demo": "1.0.5", _CONNECTOR: "1.2.1"})
    uncovered = release_train_check.uncovered_moves(repo, "main")
    assert list(uncovered) == ["plugins/fresh"]
    assert uncovered["plugins/fresh"].base_version is None
    code, _out, err = _run(repo, capsys)
    assert code == 1
    assert "but this release does not release plugins/fresh (main carries no plugins/fresh/pyproject.toml)" in err


def test_a_unit_that_becomes_a_package_names_the_file_the_base_lacks(repo: Path, capsys):
    _write(
        repo / _CONNECTOR / "pyproject.toml",
        '[project]\nname = "tai42-connector-demo"\nversion = "1.2.0"\ndependencies = ["tai42-contract>=2.3,<3"]\n',
    )
    _git(repo, "add", "--all")
    _release(repo, {"core/contract": "3.0.0", "core/kit": "5.1.1", "plugins/demo": "1.0.5"})
    code, _out, err = _run(repo, capsys)
    assert code == 1
    assert f"but this release does not release {_CONNECTOR} (main carries no {_CONNECTOR}/pyproject.toml)" in err


def test_a_base_that_is_no_commit_raises(repo: Path):
    with pytest.raises(ValueError, match="'no-such-ref' is not a commit"):
        release_train_check.uncovered_moves(repo, "no-such-ref")


def test_a_moved_unit_that_declares_no_version_raises(repo: Path):
    descriptor = repo / _CONNECTOR / "tai-plugin.yml"
    descriptor.write_text("contract: '>=2.3,<3'\n")
    _release(repo, {"core/contract": "3.0.0", "core/kit": "5.1.1", "plugins/demo": "1.0.5"})
    with pytest.raises(ValueError, match=f"{_CONNECTOR}/tai-plugin.yml declares no top-level version"):
        release_train_check.uncovered_moves(repo, "main")
