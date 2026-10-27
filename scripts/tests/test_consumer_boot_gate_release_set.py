"""Tests for which first-party plugins a release tree boots: a plugin released with changes of its own
is left to its own lanes, a plugin released for its version and derived ranges alone is booted at its
published version. Each case is a throwaway git repo under ``tmp_path`` with release tags; PyPI is
stubbed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import consumer_boot_gate as gate
from _consumer_boot_gate import consumers as cbg_consumers

_CONTRACT = "core/contract"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, timeout=60)


def _pyproject(name: str, version: str, contract_range: str | None) -> str:
    deps = f'\n    "tai42-contract{contract_range}",\n' if contract_range else ""
    return f'[project]\nname = "{name}"\nversion = "{version}"\ndependencies = [{deps}]\n'


class _Tree:
    """A released workspace: ``core/contract`` at 1.2.0 and two plugins that pin it, all tagged."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.versions = {_CONTRACT: "1.2.0", "plugins/alpha": "3.0.0", "plugins/beta": "5.1.0"}
        self.names = {_CONTRACT: "tai42-contract", "plugins/alpha": "tai42-alpha", "plugins/beta": "tai42-beta"}
        self.ranges = {"plugins/alpha": ">=1.2,<2", "plugins/beta": ">=1.2,<2"}
        root.mkdir()
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "alice@example.com")
        _git(root, "config", "user.name", "Alice")
        _git(root, "config", "commit.gpgsign", "false")
        packages = {path: {"package-name": name} for path, name in self.names.items()}
        (root / "release-please-config.json").write_text(json.dumps({"packages": packages}))
        self._write()
        self.commit("release")
        for path, version in self.versions.items():
            _git(root, "tag", f"{self.names[path]}-v{version}")

    def _write(self) -> None:
        (self.root / ".release-please-manifest.json").write_text(json.dumps(self.versions))
        for path, name in self.names.items():
            member = self.root / path
            member.mkdir(parents=True, exist_ok=True)
            (member / "pyproject.toml").write_text(_pyproject(name, self.versions[path], self.ranges.get(path)))

    def release(self, versions: dict[str, str], ranges: dict[str, str] | None = None) -> None:
        self.versions.update(versions)
        self.ranges.update(ranges or {})
        self._write()

    def change_source(self, path: str) -> None:
        (self.root / path / "module.py").write_text("VALUE = 2\n")

    def commit(self, message: str) -> None:
        _git(self.root, "add", "--all")
        _git(self.root, "commit", "-qm", message)


@pytest.fixture
def tree(tmp_path: Path) -> _Tree:
    return _Tree(tmp_path / "repo")


@pytest.fixture
def published(monkeypatch) -> dict[str, str]:
    versions = {"tai42-alpha": "3.0.0", "tai42-beta": "5.1.0"}
    monkeypatch.setattr(cbg_consumers, "latest_pypi_version", lambda name: versions[name])
    return versions


def test_a_plugin_released_with_its_own_changes_is_not_booted(tree: _Tree, published):
    tree.change_source("plugins/alpha")
    tree.release({"plugins/alpha": "3.1.0"})
    tree.commit("chore: release main")
    released = gate.released_packages(tree.root)
    assert released.own_changes == {"tai42-alpha"}
    assert released.bookkeeping_only == frozenset()
    consumers, _notices = gate.enumerate_first_party(tree.root)
    assert [consumer.install_arg for consumer in consumers] == ["tai42-beta==5.1.0"]


def test_a_plugin_released_for_its_ranges_alone_is_booted_at_its_published_version(tree: _Tree, published):
    # A contract minor moves the floor of both plugins' ranges; alpha also carries a fix of
    # its own, beta is released for the moved range alone.
    tree.change_source(_CONTRACT)
    tree.change_source("plugins/alpha")
    tree.release(
        {_CONTRACT: "1.3.0", "plugins/alpha": "3.0.1", "plugins/beta": "5.1.1"},
        {"plugins/alpha": ">=1.3,<2", "plugins/beta": ">=1.3,<2"},
    )
    tree.commit("chore: release main")
    released = gate.released_packages(tree.root)
    assert released.own_changes == {"tai42-contract", "tai42-alpha"}
    assert released.bookkeeping_only == {"tai42-beta"}
    consumers, notices = gate.enumerate_first_party(tree.root)
    assert [consumer.install_arg for consumer in consumers] == ["tai42-beta==5.1.0"]
    assert any("tai42-beta" in notice and "booting its published 5.1.0" in notice for notice in notices)


def test_a_core_major_boots_the_range_only_plugin_and_accepts_its_conflict_loudly(
    tree: _Tree, published, tmp_path: Path, capsys
):
    # Under a contract major the published beta 5.1.0 declares tai42-contract>=1.2,<2, which
    # excludes the candidate 2.0.0: it is still booted, its install finds no solution, and the
    # gate reports it as an accepted major-bump break by name — never a silent skip.
    tree.change_source(_CONTRACT)
    tree.release(
        {_CONTRACT: "2.0.0", "plugins/alpha": "3.0.1", "plugins/beta": "5.1.1"},
        {"plugins/alpha": ">=2.0,<3", "plugins/beta": ">=2.0,<3"},
    )
    tree.commit("chore: release main")
    consumers, _notices = gate.enumerate_first_party(tree.root)
    assert [consumer.install_arg for consumer in consumers] == ["tai42-alpha==3.0.0", "tai42-beta==5.1.0"]
    candidate = gate.read_candidate(tree.root, [_CONTRACT])
    assert candidate.bump == "major"
    stderr = (
        "No solution found when resolving dependencies:\n"
        "  Because tai42-beta==5.1.0 depends on tai42-contract>=1.2,<2, we can conclude that your\n"
        "  requirements are unsatisfiable.\n"
    )
    identity = gate.IdentityFixture(dir="plugins/id", dist_name="id-plugin", providers=(), lifecycle=())
    gate._report_unresolvable_consumers(
        "gate",
        candidate,
        [consumers[1]],
        stderr,
        repo_root=tree.root,
        core_dirs=[_CONTRACT],
        identity=identity,
        venv=tmp_path / "venv",
    )
    out = capsys.readouterr().out
    assert "::notice::" in out
    assert "ACCEPTED as a major-bump break: tai42-beta 5.1.0" in out
    assert "gate passes" in out


def test_a_released_member_unchanged_since_its_previous_tag_fails_loudly(tree: _Tree, published, capsys):
    # The manifest says alpha releases 3.0.1, but its dir still carries 3.0.0's tree.
    tree.versions["plugins/alpha"] = "3.0.1"
    (tree.root / ".release-please-manifest.json").write_text(json.dumps(tree.versions))
    tree.commit("chore: release main")
    with pytest.raises(SystemExit):
        gate.released_packages(tree.root)
    assert "plugins/alpha is unchanged since tai42-alpha-v3.0.0" in capsys.readouterr().err
