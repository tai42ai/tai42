"""Tests for scripts/range_sync.py: contract-pin rewriting and descriptor discovery / follow-the-pin behaviour.

The shared sample-tree builders live in _range_sync_support."""

from __future__ import annotations

from pathlib import Path

import pytest
from _range_sync_support import (
    _CONNECTOR_YML,
    _CONTRACTLESS_SCAFFOLD_YML,
    _DESC_YMLS,
    _SCAFFOLD_YML,
    _UNSHIPPED_YMLS,
    _build_descriptor_only_tree,
    _build_descriptor_pin_tree,
    _build_scaffold_tree,
    _build_underivable_pin_tree,
)

import range_sync


@pytest.mark.parametrize("quote", ["'", '"'])
def test_contract_yaml_quote_style_preserved(quote: str):
    text = f"contract: {quote}>=0.2,<0.4{quote}\n"
    new_text, changed = range_sync.rewrite_contract_yaml(text, ">=0.3,<0.4")
    assert changed
    assert new_text == f"contract: {quote}>=0.3,<0.4{quote}\n"


def test_contract_yaml_noop_when_already_synced():
    text = "contract: '>=0.3,<0.4'\n"
    new_text, changed = range_sync.rewrite_contract_yaml(text, ">=0.3,<0.4")
    assert not changed
    assert new_text == text


def test_descriptor_follows_pin(tmp_path: Path):
    _build_descriptor_pin_tree(tmp_path)
    report = range_sync.apply(tmp_path)

    # the pyproject dep is preserved at the pinned range ...
    demo_py = (tmp_path / "plugins/demo/pyproject.toml").read_text()
    assert "tai42-contract>=1.2,<2" in demo_py
    assert [p.dep_name for _, p in report.preserved] == ["tai42-contract"]

    # ... and BOTH descriptors stay consistent with the pin — never bumped to the
    # global >=2.0,<3 the released contract would otherwise force
    for rel in _DESC_YMLS:
        text = (tmp_path / rel).read_text()
        assert "contract: '>=1.2,<2'" in text
        assert ">=2.0,<3" not in text

    # already consistent: no descriptor drift, a follow-up check stays green
    assert report.dirty is False
    assert range_sync.check(tmp_path).dirty is False


def test_descriptor_corrected_to_pin(tmp_path: Path):
    _build_descriptor_pin_tree(tmp_path)
    # start the descriptors advertising the global major, inconsistent with the pin
    for rel in _DESC_YMLS:
        p = tmp_path / rel
        p.write_text(p.read_text().replace(">=1.2,<2", ">=2.0,<3"))

    report = range_sync.apply(tmp_path)

    for rel in _DESC_YMLS:
        assert "contract: '>=1.2,<2'" in (tmp_path / rel).read_text()
    # the correction is reported alongside the preserved pin
    assert any(new == ">=1.2,<2" for _, _, new in report.contract_changes)
    assert [p.dep_name for _, p in report.preserved] == ["tai42-contract"]
    assert range_sync.check(tmp_path).dirty is False


def test_underivable_pin_leaves_descriptor_untouched(tmp_path: Path, capsys):
    _build_underivable_pin_tree(tmp_path)
    before = {rel: (tmp_path / rel).read_bytes() for rel in _DESC_YMLS}

    rc = range_sync.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out

    # both descriptor copies are byte-for-byte untouched — never forced to the
    # global >=2.0,<3 the released contract would otherwise impose
    for rel in _DESC_YMLS:
        assert (tmp_path / rel).read_bytes() == before[rel]
        assert ">=2.0,<3" not in (tmp_path / rel).read_text()

    # the leave-alone is surfaced loudly, once per descriptor copy, never silent
    assert out.count("descriptor left untouched (pinned, underivable floor)") == len(_DESC_YMLS)
    # apply exits 0 — no drift of others in this tree
    assert rc == 0

    # the dep itself is preserved under the pin, no descriptor rewrite recorded,
    # and a follow-up check stays green (the untouched descriptor is not drift)
    report = range_sync.check(tmp_path)
    assert [p.dep_name for _, p in report.preserved] == ["tai42-contract"]
    assert report.contract_changes == []
    assert sorted(y for y, _ in report.descriptor_untouched) == sorted(_DESC_YMLS)
    assert all(kept == ">=1.2,<2" for _, kept in report.descriptor_untouched)
    assert report.dirty is False


def test_check_underivable_pin_reports_untouched_no_drift(tmp_path: Path, capsys):
    _build_underivable_pin_tree(tmp_path)

    rc = range_sync.main(["--check", "--root", str(tmp_path)])
    out = capsys.readouterr().out

    # symmetric to apply: the untouched descriptor is reported, not flagged as drift
    assert "descriptor left untouched (pinned, underivable floor)" in out
    assert "WARNING" not in out

    report = range_sync.check(tmp_path)
    assert report.contract_changes == []
    assert sorted(y for y, _ in report.descriptor_untouched) == sorted(_DESC_YMLS)
    # no drift of others -> green exit
    assert report.dirty is False
    assert rc == 0


def test_discover_descriptor_only_files(tmp_path: Path):
    _build_descriptor_only_tree(tmp_path)
    found = [p.relative_to(tmp_path).as_posix() for p in range_sync.descriptor_only_contract_files(tmp_path)]
    assert found == [_CONNECTOR_YML]
    # the descriptor-only dir is never mistaken for a packaged member
    members = [m.relative_to(tmp_path).as_posix() for m in range_sync.discover_members(tmp_path)]
    assert "plugins/connector-demo" not in members


def test_apply_rewrites_descriptor_only_connector(tmp_path: Path):
    _build_descriptor_only_tree(tmp_path)
    report = range_sync.apply(tmp_path)

    # the connector's contract is bumped to the GLOBAL derived range >=2.0,<3
    assert "contract: '>=2.0,<3'" in (tmp_path / _CONNECTOR_YML).read_text()
    assert any(y == _CONNECTOR_YML and new == ">=2.0,<3" for y, _, new in report.contract_changes)

    # idempotent: a follow-up check finds no drift
    assert range_sync.check(tmp_path).dirty is False


def test_check_detects_descriptor_only_connector_drift(tmp_path: Path):
    _build_descriptor_only_tree(tmp_path)
    report = range_sync.check(tmp_path)
    assert report.dirty
    assert any(
        y == _CONNECTOR_YML and old == ">=1.1,<2" and new == ">=2.0,<3" for y, old, new in report.contract_changes
    )


def test_discover_scaffold_and_shipped_files(tmp_path: Path):
    _build_scaffold_tree(tmp_path)
    members = range_sync.discover_members(tmp_path)

    scaffolds = [p.relative_to(tmp_path).as_posix() for p in range_sync.scaffold_descriptor_files(members, tmp_path)]
    assert scaffolds == [_SCAFFOLD_YML, _CONTRACTLESS_SCAFFOLD_YML]

    # a plugin member contributes its root copy and its packaged copy, in that order
    plugin_files = [
        yml.relative_to(tmp_path).as_posix() for _, yml in range_sync.plugin_descriptor_files(members, tmp_path)
    ]
    assert plugin_files == ["plugins/demo/tai-plugin.yml", "plugins/demo/src/tai42_demo/tai-plugin.yml"]


def test_apply_rewrites_scaffold_to_global_range(tmp_path: Path):
    _build_scaffold_tree(tmp_path)
    before = {rel: (tmp_path / rel).read_bytes() for rel in (*_UNSHIPPED_YMLS, _CONTRACTLESS_SCAFFOLD_YML)}
    report = range_sync.apply(tmp_path)

    assert "contract: '>=2.0,<3'" in (tmp_path / _SCAFFOLD_YML).read_text()
    assert any(
        y == _SCAFFOLD_YML and old == ">=1.1,<2" and new == ">=2.0,<3" for y, old, new in report.contract_changes
    )

    # a scaffold with no contract range, and every copy the member does not ship,
    # are byte-identical afterwards
    for rel, original in before.items():
        assert (tmp_path / rel).read_bytes() == original, rel

    assert range_sync.check(tmp_path).dirty is False


def test_check_detects_scaffold_drift(tmp_path: Path):
    _build_scaffold_tree(tmp_path)
    report = range_sync.check(tmp_path)
    drifted = {y for y, _, _ in report.contract_changes}
    assert _SCAFFOLD_YML in drifted
    assert drifted.isdisjoint(_UNSHIPPED_YMLS)
