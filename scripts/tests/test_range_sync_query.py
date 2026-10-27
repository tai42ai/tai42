"""Tests for the range_sync query: which release units' ranges candidate versions move, and its CLI."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from _range_sync_support import _write

import range_sync


def _build_released_tree(root: Path) -> None:
    """An in-sync workspace past 1.0: contract 2.3.0; kit and a plugin that pin it; a plugin that
    pins kit only; a member with a deliberate ``[tool.range-sync]`` contract cap; a member with no
    first-party deps; and a descriptor-only connector."""
    _write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["core/*", "plugins/*"]\n')
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.3.0"
        dependencies = []
        """,
    )
    _write(
        root / "core/kit/pyproject.toml",
        """
        [project]
        name = "tai42-kit"
        version = "5.1.0"
        dependencies = ["tai42-contract>=2.3,<3"]
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "1.0.4"
        dependencies = ["tai42-contract>=2.3,<3", "tai42-kit>=5.1,<6"]
        """,
    )
    _write(root / "plugins/demo/tai-plugin.yml", "package: tai42-demo\nversion: 1.0.4\ncontract: '>=2.3,<3'\n")
    _write(
        root / "plugins/kit-only/pyproject.toml",
        """
        [project]
        name = "tai42-kit-only"
        version = "0.9.0"
        dependencies = ["tai42-kit>=5.1,<6"]
        """,
    )
    _write(
        root / "plugins/held/pyproject.toml",
        """
        [project]
        name = "tai42-held"
        version = "3.0.0"
        dependencies = ["tai42-contract>=2.3,<3"]

        [tool.range-sync]
        pinned = ["tai42-contract"]
        """,
    )
    _write(root / "plugins/held/tai-plugin.yml", "package: tai42-held\nversion: 3.0.0\ncontract: '>=2.3,<3'\n")
    _write(
        root / "plugins/standalone/pyproject.toml",
        """
        [project]
        name = "tai42-standalone"
        version = "1.0.0"
        dependencies = ["httpx>=0.28"]
        """,
    )
    _write(root / "plugins/connector-demo/tai-plugin.yml", "version: 1.2.0\ncontract: '>=2.3,<3'\n")
    assert not range_sync.check(root).dirty


def test_core_major_lists_every_dependent(tmp_path: Path):
    _build_released_tree(tmp_path)
    changed = range_sync.query(tmp_path, {"core/contract": "3.0.0"})
    assert list(changed) == ["core/kit", "plugins/connector-demo", "plugins/demo"]
    assert changed["core/kit"] == [
        range_sync.RangeChange(
            "core/kit/pyproject.toml", "tai42-contract", "tai42-contract>=2.3,<3", "tai42-contract>=3.0,<4"
        )
    ]
    assert (
        range_sync.RangeChange("plugins/demo/tai-plugin.yml", "contract", ">=2.3,<3", ">=3.0,<4")
        in (changed["plugins/demo"])
    )
    assert changed["plugins/connector-demo"] == [
        range_sync.RangeChange("plugins/connector-demo/tai-plugin.yml", "contract", ">=2.3,<3", ">=3.0,<4")
    ]


def test_core_minor_lists_every_dependent_because_the_floor_moves(tmp_path: Path):
    _build_released_tree(tmp_path)
    changed = range_sync.query(tmp_path, {"core/contract": "2.4.0"})
    # The held member's pin only guards a major crossing, so its floor follows the minor too.
    assert list(changed) == ["core/kit", "plugins/connector-demo", "plugins/demo", "plugins/held"]
    assert changed["core/kit"][0].new == "tai42-contract>=2.4,<3"


def test_core_patch_lists_nothing(tmp_path: Path):
    _build_released_tree(tmp_path)
    assert range_sync.query(tmp_path, {"core/contract": "2.3.7", "core/kit": "5.1.1"}) == {}


def test_pinned_member_is_not_listed_across_a_major(tmp_path: Path):
    _build_released_tree(tmp_path)
    assert "plugins/held" not in range_sync.query(tmp_path, {"core/contract": "3.0.0"})


def test_a_kit_major_lists_only_the_members_that_depend_on_kit(tmp_path: Path):
    _build_released_tree(tmp_path)
    assert list(range_sync.query(tmp_path, {"core/kit": "6.0.0"})) == ["plugins/demo", "plugins/kit-only"]


def test_a_tree_with_the_candidate_ranges_written_queries_clean(tmp_path: Path):
    """Applying under the candidate versions writes exactly what the query listed, after which
    the same query lists nothing."""
    _build_released_tree(tmp_path)
    candidate = {"core/contract": "3.0.0"}
    listed = range_sync.query(tmp_path, candidate)
    contract = tmp_path / "core/contract/pyproject.toml"
    contract.write_text(contract.read_text().replace('version = "2.3.0"', 'version = "3.0.0"'))
    report = range_sync.apply(tmp_path)
    written = {f"{member}/pyproject.toml" for member, _ in report.spec_changes}
    written |= {yml for yml, _, _ in report.contract_changes}
    assert written == {change.file for changes in listed.values() for change in changes}
    assert range_sync.query(tmp_path, candidate) == {}


def test_unknown_path_raises(tmp_path: Path):
    _build_released_tree(tmp_path)
    with pytest.raises(ValueError, match="not a workspace member or descriptor-only component"):
        range_sync.query(tmp_path, {"core/nonesuch": "1.0.0"})


def test_malformed_version_raises(tmp_path: Path):
    _build_released_tree(tmp_path)
    with pytest.raises(ValueError, match="not a release version"):
        range_sync.query(tmp_path, {"core/contract": "three"})


def test_query_cli_prints_the_listed_members(tmp_path: Path, monkeypatch, capsys):
    _build_released_tree(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"core/contract": "3.0.0"})))
    assert range_sync.main(["--query", "--root", str(tmp_path)]) == 0
    members = json.loads(capsys.readouterr().out)["members"]
    assert [member["path"] for member in members] == ["core/kit", "plugins/connector-demo", "plugins/demo"]
    assert members[0]["changes"] == [
        {
            "file": "core/kit/pyproject.toml",
            "subject": "tai42-contract",
            "from": "tai42-contract>=2.3,<3",
            "to": "tai42-contract>=3.0,<4",
        }
    ]


def test_query_cli_refuses_input_that_is_not_a_version_map(tmp_path: Path, monkeypatch):
    _build_released_tree(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(["core/contract", "3.0.0"])))
    with pytest.raises(ValueError, match="JSON object of path -> version strings"):
        range_sync.main(["--query", "--root", str(tmp_path)])
