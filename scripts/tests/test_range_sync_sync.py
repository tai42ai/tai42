"""Tests for scripts/range_sync.py: apply / check end to end, pin preservation across a major, and stray-pin guards.

The shared sample-tree builders live in _range_sync_support."""

from __future__ import annotations

from pathlib import Path

import pytest
from _range_sync_support import (
    _PENDING_REPIN_WINDOW,
    _build_major_tree,
    _build_tree,
    _kit_deps,
)

import range_sync


def test_apply_rewrites_and_preserves(tmp_path: Path):
    _build_tree(tmp_path)
    report = range_sync.apply(tmp_path)
    assert report.dirty

    demo_text = (tmp_path / "plugins/demo/pyproject.toml").read_text()
    deps = _kit_deps(tmp_path)

    # patch floor normalized
    assert "tai42-contract>=0.3,<0.4" in deps
    assert "tai42-contract>=0.3.0,<0.4" not in demo_text
    # extras preserved, floor raised 0.2 -> 0.3
    assert "tai42-kit[llm,jq,redis]>=0.3,<0.4" in deps
    # marker preserved
    assert "tai42-kit>=0.3,<0.4; python_version >= '3.13'" in deps
    # version-less first-party ref untouched
    assert "tai42-kit[curl]" in deps
    # third-party untouched
    assert "click>=8.3" in deps
    # [tool.uv.sources] workspace lines untouched
    assert "tai42-contract = { workspace = true }" in demo_text
    assert "tai42-kit = { workspace = true }" in demo_text

    # core/kit contract floor raised
    kit_text = (tmp_path / "core/kit/pyproject.toml").read_text()
    assert "tai42-contract>=0.3,<0.4" in kit_text

    # both descriptor copies rewritten
    for rel in ("plugins/demo/tai-plugin.yml", "plugins/demo/src/tai42_demo/tai-plugin.yml"):
        assert "contract: '>=0.3,<0.4'" in (tmp_path / rel).read_text()


def test_apply_is_idempotent(tmp_path: Path):
    _build_tree(tmp_path)
    range_sync.apply(tmp_path)
    snapshot = {p: p.read_text() for p in tmp_path.rglob("*") if p.is_file()}
    report2 = range_sync.apply(tmp_path)
    assert not report2.dirty
    after = {p: p.read_text() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == snapshot


def test_comments_and_formatting_preserved(tmp_path: Path):
    _build_tree(tmp_path)
    range_sync.apply(tmp_path)
    demo_text = (tmp_path / "plugins/demo/pyproject.toml").read_text()
    # inline comments in the dependency array survive the rewrite
    assert "# extras must be preserved verbatim" in demo_text
    assert "# version-less first-party ref must be left untouched" in demo_text


def test_core_major_bump_produces_repin_and_converges(tmp_path: Path):
    """The two-train contract: the core release train bumps a depended-upon member
    and leaves its dependents' ranges stale; the post-merge re-pin (release-repin's
    range_sync apply) rewrites every dependent and is itself a fixed point, so the
    follow-up train's re-pin PR carries a self-consistent tree."""
    _build_tree(tmp_path)
    range_sync.apply(tmp_path)  # fleet in sync at contract 0.3.0

    # The core train ships only the member bump; dependents are untouched on it.
    contract = tmp_path / "core/contract/pyproject.toml"
    contract.write_text(contract.read_text().replace('version = "0.3.0"', 'version = "1.0.0"'))

    repin = range_sync.apply(tmp_path)  # what release-repin runs on the tag
    assert repin.dirty
    repinned = {member for member, _ in repin.spec_changes}
    assert repinned == {"core/kit", "plugins/demo"}  # every dependent, none missed
    assert "tai42-contract>=1.0,<2" in (tmp_path / "core/kit/pyproject.toml").read_text()

    # The re-pin PR's own tree is self-consistent — re-applying changes nothing.
    assert not range_sync.apply(tmp_path).dirty


def test_follow_up_patch_bumps_are_repin_noop(tmp_path: Path):
    """The follow-up train bumps the re-pinned dependents by a patch; a patch never
    moves a derived range (the floor is major.minor), so range_sync finds no drift
    on that train's tags and no third train opens — the loop terminates."""
    _build_tree(tmp_path)
    range_sync.apply(tmp_path)

    for rel, old in (
        ("core/contract/pyproject.toml", 'version = "0.3.0"'),
        ("core/kit/pyproject.toml", 'version = "0.3.0"'),
        ("plugins/demo/pyproject.toml", 'version = "0.2.1"'),
    ):
        p = tmp_path / rel
        bumped = old.rsplit(".", 1)[0] + f'.{int(old.rsplit(".", 1)[1].rstrip(chr(34))) + 1}"'
        p.write_text(p.read_text().replace(old, bumped))

    assert not range_sync.check(tmp_path).dirty


def test_check_passes_when_synced(tmp_path: Path):
    _build_tree(tmp_path)
    range_sync.apply(tmp_path)
    report = range_sync.check(tmp_path)
    assert not report.dirty


def test_check_detects_drift(tmp_path: Path):
    _build_tree(tmp_path)
    report = range_sync.check(tmp_path)
    assert report.dirty
    # the stale kit contract floor (>=0.2) is reported
    stale = [c for _, c in report.spec_changes if c.dep_name == "tai42-contract"]
    assert stale
    # the stale contract pin in the descriptors is reported
    assert any(old == ">=0.2,<0.4" for _, old, _ in report.contract_changes)


def test_warnings_do_not_make_report_dirty():
    # a warning is advisory: it must never, on its own, fail the gate
    report = range_sync.SyncReport(
        spec_changes=[],
        contract_changes=[],
        preserved=[("core/kit", range_sync.Preserved("tai42-contract", ">=1.2,<2"))],
        warnings=[("core/kit", range_sync.SpecChange("tai42-contract", "a", "b"))],
        descriptor_untouched=[("plugins/demo/tai-plugin.yml", ">1.2")],
    )
    assert report.dirty is False


def test_apply_preserves_pinned_cap_across_major(tmp_path: Path):
    _build_major_tree(tmp_path, pin=True)
    report = range_sync.apply(tmp_path)
    kit_text = (tmp_path / "core/kit/pyproject.toml").read_text()
    assert "tai42-contract>=1.2,<2" in kit_text  # untouched
    assert report.spec_changes == []  # no rewrite happened
    assert [p.dep_name for _, p in report.preserved] == ["tai42-contract"]
    assert report.dirty is False
    # a follow-up check stays green — the preserved pin is not drift
    assert range_sync.check(tmp_path).dirty is False


def test_apply_rewrites_unpinned_cross_major(tmp_path: Path):
    _build_major_tree(tmp_path, pin=False)
    report = range_sync.apply(tmp_path)
    kit_text = (tmp_path / "core/kit/pyproject.toml").read_text()
    assert "tai42-contract>=2.0,<3" in kit_text  # synced as today
    assert [w.dep_name for _, w in report.warnings] == ["tai42-contract"]


def test_malformed_pin_raises_via_check(tmp_path: Path):
    _build_major_tree(tmp_path, pin=True)
    kit = tmp_path / "core/kit/pyproject.toml"
    kit.write_text(kit.read_text().replace('["tai42-contract"]', '["tai42-nonesuch"]'))
    with pytest.raises(RuntimeError, match="not first-party dependencies"):
        range_sync.check(tmp_path)


def test_tracked_workspace_declares_no_preserved_pins():
    """Outside the pending-re-pin window the tracked workspace declares no preserved
    pins, so ``check`` reports no warnings, no preserved ranges, and no untouched
    descriptors. During that window (a release-please train branch or the main push
    that opens it) first-party caps are deliberately stale until the post-tag
    release-repin, so the assertions are skipped then — mirroring test_fleet's
    window-aware cap admission. Range drift between derived and declared ranges is
    checked by the range-sync gate, not here."""
    if _PENDING_REPIN_WINDOW:
        pytest.skip("pending re-pin window: first-party caps are deliberately stale until the post-tag release-repin")
    report = range_sync.check(range_sync._repo_root())
    assert report.warnings == []
    assert report.preserved == []
    assert report.descriptor_untouched == []


def test_root_pin_table_raises_on_check(tmp_path: Path):
    _build_major_tree(tmp_path, pin=False)
    root_py = tmp_path / "pyproject.toml"
    root_py.write_text(root_py.read_text() + '\n[tool.range-sync]\npinned = ["tai42-contract"]\n')
    with pytest.raises(RuntimeError, match="pin tables belong on member pyprojects"):
        range_sync.check(tmp_path)


def test_root_pin_table_raises_on_apply(tmp_path: Path):
    _build_major_tree(tmp_path, pin=False)
    root_py = tmp_path / "pyproject.toml"
    root_py.write_text(root_py.read_text() + '\n[tool.range-sync]\npinned = ["tai42-contract"]\n')
    with pytest.raises(RuntimeError, match="pin tables belong on member pyprojects"):
        range_sync.apply(tmp_path)
