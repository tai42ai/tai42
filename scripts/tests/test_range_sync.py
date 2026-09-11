"""Unit tests for scripts/range_sync.py — the deterministic derive-and-rewrite
of first-party version ranges. Hermetic: every test builds a small sample tree
under ``tmp_path`` — the sole exception is the check over the tracked workspace,
which asserts it declares no preserved pins and so reports no warnings, no
preservation, and no untouched descriptors."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from textwrap import dedent

import pytest

import range_sync  # importable via the scripts/ path conftest.py injects

# Mirrors tests/test_fleet.py's _PENDING_REPIN_WINDOW. On a release-please train
# branch and on the main push that opens the pending-re-pin window, first-party
# caps are deliberately left stale until the post-tag release-repin lands
# (.github/workflows/release-repin.yml), so the tracked workspace legitimately
# shows cross-major cap warnings during that window; drift on development PRs is
# still gated by the range-sync `--check` step and by test_fleet's cap admission.
_PENDING_REPIN_WINDOW = not (
    os.environ.get("GITHUB_HEAD_REF", "") and not os.environ["GITHUB_HEAD_REF"].startswith("release-please--")
)

# --------------------------------------------------------------------- derive


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.3.0", ">=0.3,<0.4"),
        ("0.5.1", ">=0.5,<0.6"),
        ("0.2.2", ">=0.2,<0.3"),
        ("0.4.0", ">=0.4,<0.5"),
        # floor is ALWAYS minor precision (>=major.minor); only the cap flips at
        # the 1.0 breaking boundary (next minor pre-1.0, next major from 1.0).
        ("1.2.3", ">=1.2,<2"),
        ("1.0.0", ">=1.0,<2"),
        ("2.0.0", ">=2.0,<3"),
    ],
)
def test_derive_range(version: str, expected: str):
    assert range_sync.derive_range(version) == expected


# ------------------------------------------------------------- requirement parse


def test_parse_requirement_preserves_extras_and_marker():
    parsed = range_sync.parse_requirement("tai42-kit[llm,jq,redis]>=0.2,<0.4; python_version >= '3.13'")
    assert parsed is not None
    assert parsed.name == "tai42-kit"
    assert parsed.extras == "[llm,jq,redis]"
    assert parsed.specifier == ">=0.2,<0.4"
    assert parsed.marker == "; python_version >= '3.13'"
    assert parsed.with_specifier(">=0.3,<0.4") == ("tai42-kit[llm,jq,redis]>=0.3,<0.4; python_version >= '3.13'")


def test_parse_requirement_versionless():
    parsed = range_sync.parse_requirement("tai42-kit[curl]")
    assert parsed is not None
    assert parsed.name == "tai42-kit"
    assert parsed.extras == "[curl]"
    assert parsed.specifier == ""
    assert parsed.marker == ""


# ------------------------------------------------------------------ sample tree


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dedent(text).lstrip("\n"))


def _build_tree(root: Path) -> None:
    """A minimal but representative workspace: contract + kit cores, one plugin
    with extras / marker / version-less / [tool.uv.sources] refs, both yml copies."""
    _write(
        root / "pyproject.toml",
        """
        [tool.uv.workspace]
        members = ["core/*", "plugins/*"]

        [dependency-groups]
        dev = ["pytest>=8"]
        """,
    )
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "0.3.0"
        dependencies = ["pydantic>=2.12"]
        """,
    )
    _write(
        root / "core/kit/pyproject.toml",
        """
        [project]
        name = "tai42-kit"
        version = "0.3.0"
        dependencies = [
            "tai42-contract>=0.2,<0.4",
            "httpx>=0.28",
        ]

        [project.optional-dependencies]
        redis = ["redis>=5"]

        [tool.uv.sources]
        tai42-contract = { workspace = true }
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "0.2.1"
        dependencies = [
            # patch-level floor must normalize to the minor floor
            "tai42-contract>=0.3.0,<0.4",
            # extras must be preserved verbatim
            "tai42-kit[llm,jq,redis]>=0.2,<0.4",
            # environment marker must be preserved
            "tai42-kit>=0.2,<0.4; python_version >= '3.13'",
            # version-less first-party ref must be left untouched
            "tai42-kit[curl]",
            "click>=8.3",
        ]

        [tool.uv.sources]
        tai42-contract = { workspace = true }
        tai42-kit = { workspace = true }
        """,
    )
    contract_yaml = """
        spec_version: 1
        package: tai42-demo
        version: 0.2.1
        contract: '>=0.2,<0.4'
        """
    _write(root / "plugins/demo/tai-plugin.yml", contract_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", contract_yaml)


def _kit_deps(root: Path) -> list[str]:
    with (root / "plugins/demo/pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["dependencies"]


# ---------------------------------------------------------------------- apply


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


# ---------------------------------------------------------------------- check


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


def test_check_cli_exit_codes(tmp_path: Path):
    _build_tree(tmp_path)
    # drift present -> exit 1
    assert range_sync.main(["--check", "--root", str(tmp_path)]) == 1
    # apply -> exit 0
    assert range_sync.main(["--root", str(tmp_path)]) == 0
    # now synced -> exit 0
    assert range_sync.main(["--check", "--root", str(tmp_path)]) == 0


# ------------------------------------------------------------- yaml quote style


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


# ------------------------------------------------------- PEP 503 name matching


def test_noncanonical_dep_name_is_matched():
    # A first-party dep spelled non-canonically (underscore / mixed case) must
    # still resolve to its member so a stale range cannot false-green as "in
    # sync". The rewritten literal preserves the original spelling.
    first_party = {"tai42-kit": "0.3.0"}
    pyproject = {"project": {"dependencies": ["tai42_Kit>=0.2,<0.4"]}}
    changes = range_sync.compute_pyproject_changes(pyproject, first_party)
    assert len(changes) == 1
    assert changes[0].new_req == "tai42_Kit>=0.3,<0.4"


def test_normalize_name():
    assert range_sync._normalize_name("tai42_Kit") == "tai42-kit"
    assert range_sync._normalize_name("tai42-kit") == "tai42-kit"
    assert range_sync._normalize_name("Tai42.Contract") == "tai42-contract"


# ------------------------------------------------- cross-major pin preservation


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        (">=1.2,<2", ">=2.0,<3", True),  # 1.x -> 2.x: floor major rises
        (">=1.2,<2", ">=1.5,<2", False),  # minor bump within major 1
        (">=2.0,<3", ">=2.1,<3", False),  # minor bump within major 2
        (">=0.3,<0.4", ">=0.5,<0.6", False),  # pre-1.0: major stays 0, never cross-major
        ("", ">=2.0,<3", False),  # no old floor -> no comparable major
        (">=1.2,<2", "", False),  # no new floor -> no comparable major
        (">=1.2,<3", ">=1.5,<2", True),  # widened cap narrowed 3 -> 2 within floor major 1
        ("~=1.2", ">=2.0,<3", True),  # compat spec implies (1, 1); floor major rises
        ("==1.2.3", ">=1.5,<2", True),  # exact spec implies (1, 1); cap major differs
        ("~=1.2", ">=1.5,<2", True),  # compat (1, 1) vs (1, 2): cap major differs
    ],
)
def test_is_cross_major(old: str, new: str, expected: bool):
    assert range_sync.is_cross_major(old, new) is expected


def test_pinned_cap_preserved_across_major():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<2"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party, "core/kit")
    assert analysis.changes == []  # left untouched
    assert [(p.dep_name, p.kept_range) for p in analysis.preserved] == [("tai42-contract", ">=1.2,<2")]
    assert analysis.warnings == []


def test_unpinned_cross_major_syncs_and_warns():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {"project": {"dependencies": ["tai42-contract>=1.2,<2"]}}
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    # still rewrites (syncs as today) ...
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=2.0,<3"]
    assert analysis.preserved == []
    # ... but the unannotated cross-major cap is flagged for the --check warning
    assert [w.dep_name for w in analysis.warnings] == ["tai42-contract"]
    assert analysis.warnings[0].old_req == "tai42-contract>=1.2,<2"
    assert analysis.warnings[0].new_req == "tai42-contract>=2.0,<3"


def test_pinning_ignored_on_minor_sync():
    first_party = {"tai42-contract": "1.5.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<2"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    # a minor bump within the same major rewrites normally despite the pin
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=1.5,<2"]
    assert analysis.preserved == []
    assert analysis.warnings == []


def test_pin_name_matched_noncanonically():
    # a pin spelled non-canonically still resolves to its dependency
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<2"]},
        "tool": {"range-sync": {"pinned": ["Tai42_Contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    assert [p.dep_name for p in analysis.preserved] == ["tai42-contract"]
    assert analysis.changes == []


def test_malformed_pin_unknown_dep_raises():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<2"]},
        "tool": {"range-sync": {"pinned": ["tai42-nonesuch"]}},
    }
    with pytest.raises(RuntimeError, match="not first-party dependencies"):
        range_sync.analyze_pyproject(pyproject, first_party, "core/kit")


def test_malformed_pin_wrong_type_raises():
    pyproject = {"project": {"dependencies": []}, "tool": {"range-sync": {"pinned": "tai42-contract"}}}
    with pytest.raises(RuntimeError, match="must be a list"):
        range_sync.pinned_deps(pyproject)


def test_warnings_do_not_make_report_dirty():
    # a warning is advisory: it must never, on its own, fail the gate
    report = range_sync.SyncReport(
        spec_changes=[],
        contract_changes=[],
        preserved=[("core/kit", range_sync.Preserved("tai42-contract", ">=1.2,<2"))],
        warnings=[("core/kit", range_sync.SpecChange("tai42-contract", "a", "b"))],
        descriptor_untouched=[("plugins/demo/tai-plugin.yml", ">1.2")],
        readme_changes=[],
        readme_untouched=[("plugins/demo/README.md", "7.x contract")],
    )
    assert report.dirty is False


def _build_major_tree(root: Path, *, pin: bool) -> None:
    """A workspace where kit's declared contract cap (``<2``) is crossed by the
    released contract major (2.0.0), so the derived range is ``>=2.0,<3``."""
    _write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["core/*"]\n')
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    pin_table = '\n[tool.range-sync]\npinned = ["tai42-contract"]\n' if pin else ""
    _write(
        root / "core/kit/pyproject.toml",
        f"""
        [project]
        name = "tai42-kit"
        version = "2.0.0"
        dependencies = ["tai42-contract>=1.2,<2"]
        {pin_table}""",
    )


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


def test_check_cli_reports_preserved_and_exits_green(tmp_path: Path, capsys):
    _build_major_tree(tmp_path, pin=True)
    rc = range_sync.main(["--check", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "tai42-contract pinned, left at >=1.2,<2" in out
    assert "WARNING" not in out
    # loud signal, green exit: a preserved pin is not drift
    assert rc == 0


def test_check_cli_warns_on_unpinned_cross_major(tmp_path: Path, capsys):
    _build_major_tree(tmp_path, pin=False)
    rc = range_sync.main(["--check", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "cross-major rewrite of an unannotated cap" in out
    assert "tai42-contract" in out
    # the pending rewrite is genuine drift -> exit 1; the warning itself never drives it
    assert rc == 1


def test_malformed_pin_raises_via_check(tmp_path: Path):
    _build_major_tree(tmp_path, pin=True)
    kit = tmp_path / "core/kit/pyproject.toml"
    kit.write_text(kit.read_text().replace('["tai42-contract"]', '["tai42-nonesuch"]'))
    with pytest.raises(RuntimeError, match="not first-party dependencies"):
        range_sync.check(tmp_path)


def test_tracked_workspace_declares_no_preserved_pins():
    """Outside the pending-re-pin window the tracked workspace declares no preserved
    pins, so ``check`` reports no warnings, no preserved ranges, and no untouched
    descriptors or READMEs. During that window (a release-please train branch or the main push
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
    assert report.readme_untouched == []


# ---------------------------------------- guarded rewrite: widened cap / compat


def test_widened_cap_pin_preserved():
    # a DELIBERATELY WIDENED cap (<3) that the derived range narrows (<2) is a
    # cross at the cap end even though the floor major (1) does not move
    first_party = {"tai42-contract": "1.5.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>=1.2,<3"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party, "core/kit")
    assert analysis.changes == []  # pin honoured; the widened cap is not narrowed
    assert [(p.dep_name, p.kept_range) for p in analysis.preserved] == [("tai42-contract", ">=1.2,<3")]
    assert analysis.warnings == []


def test_widened_cap_unpinned_syncs_and_warns():
    first_party = {"tai42-contract": "1.5.0"}
    pyproject = {"project": {"dependencies": ["tai42-contract>=1.2,<3"]}}
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=1.5,<2"]
    assert analysis.preserved == []
    assert [w.dep_name for w in analysis.warnings] == ["tai42-contract"]


def test_compat_spec_pin_preserved():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract~=1.2"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party, "core/kit")
    assert analysis.changes == []
    assert [p.kept_range for p in analysis.preserved] == ["~=1.2"]
    assert analysis.warnings == []


def test_exact_spec_unpinned_syncs_and_warns():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {"project": {"dependencies": ["tai42-contract==1.2.3"]}}
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=2.0,<3"]
    assert analysis.preserved == []
    assert [w.dep_name for w in analysis.warnings] == ["tai42-contract"]


def test_unparseable_spec_pin_preserved():
    # a spec with no parseable major structure (bare ``>``) is preserved
    # conservatively under a pin rather than rewritten blind
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {
        "project": {"dependencies": ["tai42-contract>1.2"]},
        "tool": {"range-sync": {"pinned": ["tai42-contract"]}},
    }
    analysis = range_sync.analyze_pyproject(pyproject, first_party, "core/kit")
    assert analysis.changes == []
    assert [p.kept_range for p in analysis.preserved] == [">1.2"]
    assert analysis.warnings == []


def test_unparseable_spec_unpinned_syncs_and_warns():
    first_party = {"tai42-contract": "2.0.0"}
    pyproject = {"project": {"dependencies": ["tai42-contract>1.2"]}}
    analysis = range_sync.analyze_pyproject(pyproject, first_party)
    assert [c.new_req for c in analysis.changes] == ["tai42-contract>=2.0,<3"]
    assert [w.dep_name for w in analysis.warnings] == ["tai42-contract"]


# -------------------------------------------------- descriptor follows the pin


def _build_descriptor_pin_tree(root: Path) -> None:
    """A plugin that pins ``tai42-contract`` to ``>=1.2,<2`` while the released
    contract is 2.0.0 (global derived range ``>=2.0,<3``). Both descriptor
    copies start consistent with the pin."""
    _write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["core/*", "plugins/*"]\n')
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "0.1.0"
        dependencies = ["tai42-contract>=1.2,<2"]

        [tool.range-sync]
        pinned = ["tai42-contract"]
        """,
    )
    contract_yaml = """
        spec_version: 1
        package: tai42-demo
        version: 0.1.0
        contract: '>=1.2,<2'
        """
    _write(root / "plugins/demo/tai-plugin.yml", contract_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", contract_yaml)


_DESC_YMLS = ("plugins/demo/tai-plugin.yml", "plugins/demo/src/tai42_demo/tai-plugin.yml")


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


def _build_underivable_pin_tree(root: Path) -> None:
    """A plugin that pins ``tai42-contract`` with a spec that has NO derivable
    floor (bare ``>1.2``) while the released contract is 2.0.0 (global derived
    range ``>=2.0,<3``). The pin preserves the dep, but no floor can be derived
    to re-range the descriptor — so both descriptor copies must be left
    untouched entirely, never forced to the global range the pin refuses."""
    _write(root / "pyproject.toml", '[tool.uv.workspace]\nmembers = ["core/*", "plugins/*"]\n')
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "0.1.0"
        dependencies = ["tai42-contract>1.2"]

        [tool.range-sync]
        pinned = ["tai42-contract"]
        """,
    )
    contract_yaml = """
        spec_version: 1
        package: tai42-demo
        version: 0.1.0
        contract: '>=1.2,<2'
        """
    _write(root / "plugins/demo/tai-plugin.yml", contract_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", contract_yaml)


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


# ------------------------------------ descriptor-only connector follows global


def _build_descriptor_only_tree(root: Path) -> None:
    """A workspace with a DESCRIPTOR-ONLY connector: a ``plugins/*`` dir carrying
    a ``tai-plugin.yml`` but NO ``pyproject.toml`` (listed under the root's
    ``[tool.uv.workspace].exclude``). It ships no package, so it is never a member
    — yet its ``contract:`` still tracks the global released contract range. Here
    the released contract is 2.0.0 (global derived ``>=2.0,<3``) while the
    connector advertises a stale ``>=1.1,<2``."""
    _write(
        root / "pyproject.toml",
        """
        [tool.uv.workspace]
        members = ["core/*", "plugins/*"]
        exclude = ["plugins/connector-demo"]
        """,
    )
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    _write(
        root / "plugins/connector-demo/tai-plugin.yml",
        """
        spec_version: 1
        namespace: tai42
        name: connector-demo
        version: 1.0.0
        contract: '>=1.1,<2'
        """,
    )


_CONNECTOR_YML = "plugins/connector-demo/tai-plugin.yml"


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


# ----------------------------------------- shipped scaffolds follow the global


def _build_scaffold_tree(root: Path) -> None:
    """A workspace whose CLI member ships plugin SCAFFOLDS as package data under
    its ``src/`` tree: one scaffold declaring a ``contract:`` range, one declaring
    none. Beside them sit copies that are NOT shipped — a build output and a test
    fixture, under the CLI and under a packaged plugin. The released contract is
    2.0.0 (global derived ``>=2.0,<3``) while every descriptor on disk advertises a
    stale ``>=1.1,<2``."""
    _write(
        root / "pyproject.toml",
        """
        [tool.uv.workspace]
        members = ["core/*", "plugins/*"]
        """,
    )
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    _write(
        root / "core/cli/pyproject.toml",
        """
        [project]
        name = "tai42-cli"
        version = "2.0.0"
        dependencies = ["tai42-contract>=2.0,<3"]
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "1.0.0"
        dependencies = ["tai42-contract>=2.0,<3"]
        """,
    )
    stale_yaml = """
        spec_version: 1
        namespace: acme
        name: demo
        version: 1.0.0
        contract: '>=1.1,<2'
        """
    _write(root / "core/cli/src/tai42_cli/templates/connector/tai-plugin.yml", stale_yaml)
    _write(
        root / "core/cli/src/tai42_cli/templates/server/tai-plugin.yml",
        """
        spec_version: 1
        namespace: acme
        name: server
        version: 1.0.0
        """,
    )
    _write(root / "core/cli/build/lib/tai42_cli/templates/connector/tai-plugin.yml", stale_yaml)
    _write(root / "core/cli/tests/fixtures/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/demo/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/demo/build/lib/tai42_demo/tai-plugin.yml", stale_yaml)


_SCAFFOLD_YML = "core/cli/src/tai42_cli/templates/connector/tai-plugin.yml"
_CONTRACTLESS_SCAFFOLD_YML = "core/cli/src/tai42_cli/templates/server/tai-plugin.yml"
_UNSHIPPED_YMLS = (
    "core/cli/build/lib/tai42_cli/templates/connector/tai-plugin.yml",
    "core/cli/tests/fixtures/tai-plugin.yml",
    "plugins/demo/build/lib/tai42_demo/tai-plugin.yml",
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


# ------------------------------------------------- stray pin table is loud


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


# ------------------------------------------------------ apply-mode stdout


def test_apply_cli_prints_preserved_and_no_warnings(tmp_path: Path, capsys):
    _build_major_tree(tmp_path, pin=True)
    rc = range_sync.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    # the preserved pin is surfaced loudly in apply mode ...
    assert "tai42-contract pinned, left at >=1.2,<2" in out
    # ... while warnings are deliberately suppressed outside --check
    assert "WARNING" not in out
    assert rc == 0


def test_apply_cli_suppresses_warnings_on_unpinned_cross_major(tmp_path: Path, capsys):
    _build_major_tree(tmp_path, pin=False)
    rc = range_sync.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out
    # the unpinned cross-major cap is synced but its warning is not printed in apply mode
    assert "WARNING" not in out
    assert rc == 0


# ------------------------------------------- shipped README contract statements


_STATEMENT = "The current release line tracks the **1.x contract** (`tai42-contract>=1.1,<2`).\n"
_STATEMENT_SYNCED = "The current release line tracks the **2.x contract** (`tai42-contract>=2.0,<3`).\n"


def _build_readme_tree(root: Path) -> None:
    """A workspace whose shipped READMEs state the contract line: a PLUGIN member's
    package page (which must carry the statement), a core README carrying the
    parenthetical shape beside a fenced diagram, the README beside a
    descriptor-only connector, and the README beside a shipped scaffold. Beside
    them sit READMEs nothing ships — a build output and a test fixture — and one
    member README whose prose states no contract line at all. The released
    contract is 2.0.0 (global derived ``>=2.0,<3``) while every statement on disk
    says 1.x."""
    _write(
        root / "pyproject.toml",
        """
        [tool.uv.workspace]
        members = ["core/*", "plugins/*"]
        exclude = ["plugins/connector-demo"]
        """,
    )
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        readme = "README.md"
        dependencies = []
        """,
    )
    # version-like prose that states no contract line: must survive byte-for-byte
    _write(
        root / "core/contract/README.md",
        """
        # tai42-contract

        Requires **Python 3.13+** and ships `pydantic>=2.12`. The 8.x dotted event
        spellings are gone, and the 1.x correlation helpers with them.
        """,
    )
    _write(
        root / "core/cli/pyproject.toml",
        """
        [project]
        name = "tai42-cli"
        version = "2.0.0"
        readme = "README.md"
        dependencies = ["tai42-contract>=2.0,<3"]
        """,
    )
    _write(
        root / "core/cli/README.md",
        """
        ```text
        tai42-contract  <--  tai42-cli
        ```

        `tai42-cli` obeys the leaf rule: its only tai-* dependency is `tai42-contract`
        (the 1.x contract line).
        """,
    )
    stale_yaml = """
        spec_version: 1
        namespace: acme
        name: demo
        version: 1.0.0
        contract: '>=1.1,<2'
        """
    _write(root / "core/cli/src/tai42_cli/templates/connector/tai-plugin.yml", stale_yaml)
    _write(root / "core/cli/src/tai42_cli/templates/connector/README.md", _STATEMENT)
    _write(root / "core/cli/build/lib/tai42_cli/templates/connector/README.md", _STATEMENT)
    _write(root / "core/cli/tests/fixtures/README.md", _STATEMENT)
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "1.0.0"
        readme = "README.md"
        dependencies = ["tai42-contract>=2.0,<3"]
        """,
    )
    _write(root / "plugins/demo/README.md", _STATEMENT)
    _write(root / "plugins/demo/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/connector-demo/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/connector-demo/README.md", _STATEMENT)


# The governed READMEs of that tree: path -> (member whose range it follows, is
# the statement mandatory). None = the global range, as its descriptor follows.
_GOVERNED_READMES = {
    "core/cli/README.md": ("core/cli", False),
    "core/cli/src/tai42_cli/templates/connector/README.md": (None, False),
    "core/contract/README.md": ("core/contract", False),
    "plugins/connector-demo/README.md": (None, False),
    "plugins/demo/README.md": ("plugins/demo", True),
}
_UNSHIPPED_READMES = (
    "core/cli/build/lib/tai42_cli/templates/connector/README.md",
    "core/cli/tests/fixtures/README.md",
)
_DRIFTED_READMES = {rel for rel in _GOVERNED_READMES if rel != "core/contract/README.md"}


def test_readme_contract_files_governed(tmp_path: Path):
    _build_readme_tree(tmp_path)
    members = range_sync.discover_members(tmp_path)
    governed = {
        g.path.relative_to(tmp_path).as_posix(): (g.member_path, g.required)
        for g in range_sync.readme_contract_files(members, tmp_path)
    }
    assert governed == _GOVERNED_READMES


def test_apply_rewrites_shipped_readme_statements(tmp_path: Path):
    _build_readme_tree(tmp_path)
    untouched = {rel: (tmp_path / rel).read_bytes() for rel in (*_UNSHIPPED_READMES, "core/contract/README.md")}
    report = range_sync.apply(tmp_path)

    # the plugin's package page, the scaffold's README and the descriptor-only
    # connector's all state the released major
    for rel in (
        "plugins/demo/README.md",
        "core/cli/src/tai42_cli/templates/connector/README.md",
        "plugins/connector-demo/README.md",
    ):
        assert (tmp_path / rel).read_text() == _STATEMENT_SYNCED, rel

    # the parenthetical shape moves in its own wording, and the fenced diagram
    # beside it is untouched
    cli_readme = (tmp_path / "core/cli/README.md").read_text()
    assert "(the 2.x contract line)." in cli_readme
    assert "tai42-contract  <--  tai42-cli" in cli_readme

    # a README stating no contract line, and every README nothing ships, are
    # byte-identical afterwards
    for rel, original in untouched.items():
        assert (tmp_path / rel).read_bytes() == original, rel

    assert ("plugins/demo/README.md", _STATEMENT.strip(), _STATEMENT_SYNCED.strip()) in report.readme_changes

    # idempotent: re-applying finds nothing and writes nothing
    snapshot = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert not range_sync.apply(tmp_path).dirty
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == snapshot


def test_check_detects_readme_drift(tmp_path: Path):
    _build_readme_tree(tmp_path)
    report = range_sync.check(tmp_path)
    assert report.dirty

    drifted = {path for path, _, _ in report.readme_changes}
    assert drifted == _DRIFTED_READMES
    assert drifted.isdisjoint(_UNSHIPPED_READMES)
    assert ("plugins/demo/README.md", _STATEMENT.strip(), _STATEMENT_SYNCED.strip()) in report.readme_changes


def test_readme_follows_preserved_pin(tmp_path: Path):
    """A member whose contract dep a pin preserves states the PINNED major in its
    README, exactly as its descriptor does — never the global released one."""
    _build_descriptor_pin_tree(tmp_path)
    _write(tmp_path / "plugins/demo/README.md", _STATEMENT_SYNCED)  # the global >=2.0,<3

    range_sync.apply(tmp_path)

    assert (tmp_path / "plugins/demo/README.md").read_text() == (
        "The current release line tracks the **1.x contract** (`tai42-contract>=1.2,<2`).\n"
    )
    assert range_sync.check(tmp_path).dirty is False


def test_underivable_pin_leaves_readme_untouched(tmp_path: Path, capsys):
    """A pin with no derivable floor leaves the README alone, loudly — the same
    leave-alone its descriptor gets, never a guessed major."""
    _build_underivable_pin_tree(tmp_path)
    pinned = "The current release line tracks the **1.x contract** (`tai42-contract>=1.2,<2`).\n"
    _write(tmp_path / "plugins/demo/README.md", pinned)
    before = (tmp_path / "plugins/demo/README.md").read_bytes()

    rc = range_sync.main(["--root", str(tmp_path)])
    out = capsys.readouterr().out

    assert (tmp_path / "plugins/demo/README.md").read_bytes() == before
    assert "plugins/demo/README.md: contract statement left untouched (pinned, underivable floor)" in out
    assert rc == 0

    report = range_sync.check(tmp_path)
    assert report.readme_changes == []
    assert report.readme_untouched == [("plugins/demo/README.md", pinned.strip())]
    assert report.dirty is False


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # the release-line sentence
        (
            "The current release line tracks the **1.x contract** (`tai42-contract>=1.1,<2`).",
            "The current release line tracks the **2.x contract** (`tai42-contract>=2.0,<3`).",
        ),
        # extras on the literal are carried over
        (
            "The current release line tracks the **1.x contract** (`tai42-contract[all]>=1.1,<2`).",
            "The current release line tracks the **2.x contract** (`tai42-contract[all]>=2.0,<3`).",
        ),
        # a version-less literal keeps its missing specifier
        (
            "The current release line tracks the **1.x contract** (`tai42-contract`).",
            "The current release line tracks the **2.x contract** (`tai42-contract`).",
        ),
        # the parenthetical shape, which carries no range of its own
        (
            "its only dependency is `tai42-contract` (the 1.x contract line).",
            "its only dependency is `tai42-contract` (the 2.x contract line).",
        ),
        # already current
        (
            "The current release line tracks the **2.x contract** (`tai42-contract>=2.0,<3`).",
            "The current release line tracks the **2.x contract** (`tai42-contract>=2.0,<3`).",
        ),
    ],
)
def test_rewrite_readme_contract_preserves_shape(text: str, expected: str):
    assert range_sync.rewrite_readme_contract(text, ">=2.0,<3")[0] == expected


def test_rewrite_readme_contract_reports_the_statement():
    new_text, changes = range_sync.rewrite_readme_contract(
        _STATEMENT, ">=2.0,<3", "plugins/demo/README.md", required=True
    )
    assert changes == [(_STATEMENT.strip(), _STATEMENT_SYNCED.strip())]
    assert new_text == _STATEMENT_SYNCED


@pytest.mark.parametrize(
    "text",
    [
        # a fenced block showing a package page's wording
        "```markdown\nThe current release line tracks the **1.x contract** (`tai42-contract>=1.1,<2`).\n```\n",
        # a fenced block of the requirement itself
        '```toml\ndependencies = ["tai42-contract>=1.1,<2"]\n```\n',
        # an inline code span quoting the phrase
        "Write it as ``the 1.x contract`` in the header.\n",
        # an HTML comment
        "<!-- the 1.x contract line -->\n",
    ],
)
def test_quoted_contract_majors_are_never_rewritten(text: str):
    """Quoted material states nothing: a fence, a code span and an HTML comment
    come back byte-identical with no change recorded."""
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    assert new_text == text
    assert changes == []


def test_wide_fence_is_not_closed_by_a_narrower_run():
    """A fence closes on a run of its own character that is at least as long as
    the opening one: a shorter run inside a wider fence is body text, so the whole
    block stays quoted — statement-shaped lines and contract facts alike."""
    text = (
        "````text\n"
        "The current release line tracks the **1.x contract** (`tai42-contract>=1.1,<2`).\n"
        "```\n"
        "Pin tai42-contract>=1.1,<2 by hand.\n"
        "````\n"
    )
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    assert new_text == text
    assert changes == []


def test_indented_code_block_is_quoted():
    """A chunk indented four columns after a blank line is a code block: the
    statement-shaped line in it is an example, not a claim."""
    text = "Every package page carries:\n\n    " + _STATEMENT
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    assert new_text == text, "rewrote a statement inside an indented code block"
    assert changes == []


def test_tab_indented_code_block_is_quoted():
    """A tab indents four columns, so a tab-indented chunk is a code block too."""
    text = "Every package page carries:\n\n\t" + _STATEMENT
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    assert new_text == text, "rewrote a statement inside a tab-indented code block"
    assert changes == []


def test_indented_continuation_of_a_paragraph_is_prose():
    """An indented chunk cannot interrupt a paragraph: with no blank line before
    it, it is a wrapped sentence and the statement in it is governed."""
    text = "The line to know:\n    " + _STATEMENT
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    assert new_text == "The line to know:\n    " + _STATEMENT_SYNCED
    assert changes == [(_STATEMENT.strip(), _STATEMENT_SYNCED.strip())]


@pytest.mark.parametrize(
    ("prefix", "quoted"),
    [
        # a tab is four columns BEFORE a blockquote marker as much as after one,
        # so each of these indents a code block rather than opening a quote
        ("\t> ", True),
        ("\t\t> ", True),
        (" \t> ", True),
        ("  \t> ", True),
        ("\t>> ", True),
        ("   \t> ", True),  # three spaces already exhaust a marker's indentation
        ("\t", True),  # the plain tab-indented block
        # a marker at three columns or fewer opens a quote, whose content is prose
        (">\t", False),  # a tab AFTER the marker is padding, not indentation
        ("> ", False),
        ("   > ", False),
        ("    > ", True),  # four columns is a code block, marker or not
    ],
)
def test_tab_columns_decide_quote_or_code(prefix: str, quoted: bool):
    """Columns, not characters, decide whether a line opens a quote or indents a
    code block — the same count on both sides of a blockquote marker."""
    text = "para\n\n" + prefix + _STATEMENT
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    if quoted:
        assert new_text == text, f"rewrote a statement in a code block prefixed {prefix!r}"
        assert changes == []
    else:
        assert new_text == "para\n\n" + prefix + _STATEMENT_SYNCED, f"left quoted prose ungoverned: {prefix!r}"
        assert changes == [(_STATEMENT.strip(), _STATEMENT_SYNCED.strip())]


def test_code_inside_a_blockquote_is_quoted():
    """Blockquote markers are stripped before blocks are read, so a fence one
    level in is a fence — with no backticks of its own to pair by luck."""
    text = "> ~~~\n> " + _STATEMENT.replace("`", "'") + "> ~~~\n"
    statement = "The current release line tracks the **1.x contract** ('tai42-contract>=1.1,<2')."
    assert statement in text  # the fenced body is the sentence with plain quotes
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    assert new_text == text, "read a blockquoted code block as prose"
    assert changes == []


def test_code_indented_under_a_list_item_is_quoted():
    """A fenced example indented under a list item sits at four columns, so it is
    read as a code block — quoted, as a renderer shows it."""
    text = "- Example:\n\n      ~~~\n      " + _STATEMENT + "      ~~~\n"
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    assert new_text == text, "rewrote a statement inside a list item's code block"
    assert changes == []


def test_escaped_backtick_does_not_delimit_a_code_span():
    """A backslash escapes the backtick it precedes, so it never pairs with a real
    delimiter and leaves the statement that follows governed."""
    text = "Escaped \\` and `code` here.\n\n" + _STATEMENT
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    assert new_text == "Escaped \\` and `code` here.\n\n" + _STATEMENT_SYNCED, (
        "an escaped backtick left the statement ungoverned"
    )
    assert changes == [(_STATEMENT.strip(), _STATEMENT_SYNCED.strip())]


def test_closing_fence_may_not_carry_an_info_string():
    """A run carrying an info string opens a nested example rather than closing
    the block, so the block's body — a statement-shaped line included — stays
    quoted until the bare run that really closes it."""
    text = (
        "```markdown\n"
        "```python\n"
        "The current release line tracks the **1.x contract** (`tai42-contract>=1.1,<2`).\n"
        "```\n"
    )
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")
    assert new_text == text, "rewrote a statement inside a fenced block"
    assert changes == []


@pytest.mark.parametrize(
    "text",
    [
        # the idiom about a past line, with no dependency in front of it
        "Topics moved in 9.0 (the 9.x contract line).\n",
        # the idiom about another package's line
        "Extensions follow `tai42-kit` (the 3.x contract line).\n",
        # a blank line between: a new paragraph, not the dependency's qualifier
        "`tai42-contract` is the leaf.\n\n(the 9.x contract line) was dropped.\n",
    ],
)
def test_parenthetical_without_its_antecedent_is_refused(text: str):
    """The parenthetical names the line of the ``tai42-contract`` dependency right
    before it; with no such antecedent it is not a statement, and the version it
    states is refused rather than restated."""
    with pytest.raises(RuntimeError, match="sits outside the contract statement"):
        range_sync.rewrite_readme_contract(text, ">=2.0,<3", "core/kit/README.md")


def test_parenthetical_rewrites_across_its_line_break():
    """The antecedent may sit on the line above; only the parenthetical moves."""
    text = "its only tai-* dependency is `tai42-contract`\n(the 1.x contract line). It\n"
    new_text, changes = range_sync.rewrite_readme_contract(text, ">=2.0,<3", "core/kit/README.md")
    assert new_text == "its only tai-* dependency is `tai42-contract`\n(the 2.x contract line). It\n"
    assert changes == [("(the 1.x contract line)", "(the 2.x contract line)")]


@pytest.mark.parametrize(
    "text",
    [
        # a compatibility table: two majors a noun-phrase rewrite would flatten
        "| plugin | contract |\n| --- | --- |\n| 3.x | 9.x contract |\n| 4.x | 10.x contract |\n",
        # an upgrade note naming both ends
        "Upgrading from the 9.x contract to the 10.x contract renames the topics.\n",
        # a possessive historical clause
        "The 9.x contract's dotted spellings were unregisterable.\n",
        # a major belonging to a different package
        "Extensions follow the 3.x contract of `tai42-kit`.\n",
        # the article's tail inside a word is not the article
        "(lathe 1.x contract line)\n",
        # a requirement stated in prose rather than in the sentence
        "Pin tai42-contract>=1.1,<2 by hand.\n",
    ],
)
def test_prose_contract_majors_outside_the_statement_are_refused(text: str):
    """A contract version in prose that is not the governed sentence is refused,
    naming the file — never rewritten to the current major."""
    with pytest.raises(RuntimeError, match="sits outside the contract statement"):
        range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md")


@pytest.mark.parametrize(
    "text",
    [
        # reworded out of the shape, still publishing a version
        "The release line tracks contract major 1.\n",
        # the requirement moved into a fenced block
        "The release line requires:\n\n```toml\ntai42-contract>=1.1,<2\n```\n",
        # the sentence reflowed across two lines
        "The current release line tracks the **1.x\ncontract** (`tai42-contract>=1.1,<2`).\n",
        # deleted outright
        "# tai42-demo\n\nA plugin.\n",
    ],
)
def test_plugin_readme_losing_its_statement_raises(text: str):
    """A plugin's package page must carry its statement: rewording, fencing,
    reflowing or deleting it is loud, never a silently ungoverned surface."""
    with pytest.raises(RuntimeError, match="no contract statement") as excinfo:
        range_sync.rewrite_readme_contract(text, ">=2.0,<3", "plugins/demo/README.md", required=True)
    assert "plugins/demo/README.md" in str(excinfo.value)


def test_reflowed_statement_raises_on_an_optional_readme():
    """Where the statement is not mandatory, a reflow still surfaces: the version
    it leaves behind in prose is a fact outside the governed sentence."""
    text = "its only tai-* dependency is `tai42-contract`\n(the 1.x\ncontract line).\n"
    with pytest.raises(RuntimeError, match="sits outside the contract statement"):
        range_sync.rewrite_readme_contract(text, ">=2.0,<3", "core/kit/README.md")


def test_two_statements_raise():
    with pytest.raises(RuntimeError, match="2 contract statements"):
        range_sync.rewrite_readme_contract(_STATEMENT + "\n" + _STATEMENT, ">=2.0,<3", "plugins/demo/README.md")


def test_ungoverned_contract_fact_raises_via_check(tmp_path: Path):
    _build_readme_tree(tmp_path)
    _write(tmp_path / "core/cli/README.md", "Pin tai42-contract>=1.1,<2 by hand.\n")
    with pytest.raises(RuntimeError, match=re.escape("core/cli/README.md")):
        range_sync.check(tmp_path)


def test_plugin_readme_deletion_raises_via_check(tmp_path: Path):
    _build_readme_tree(tmp_path)
    _write(tmp_path / "plugins/demo/README.md", "# tai42-demo\n\nA plugin.\n")
    with pytest.raises(RuntimeError, match=re.escape("plugins/demo/README.md")):
        range_sync.check(tmp_path)


def _build_pending_writes_tree(root: Path) -> None:
    """A workspace with a pending write on EVERY surface at once: one plugin whose
    pyproject specifier, whose two descriptor pins and whose README statement all
    trail the released contract (2.0.0, global derived ``>=2.0,<3``). A second
    plugin carries its statement plus a contract version in prose — the refusal,
    reached after the first plugin's three writes are pending because its path
    sorts later."""
    _write(
        root / "pyproject.toml",
        """
        [tool.uv.workspace]
        members = ["core/*", "plugins/*"]
        """,
    )
    _write(
        root / "core/contract/pyproject.toml",
        """
        [project]
        name = "tai42-contract"
        version = "2.0.0"
        dependencies = []
        """,
    )
    _write(
        root / "plugins/demo/pyproject.toml",
        """
        [project]
        name = "tai42-demo"
        version = "1.0.0"
        readme = "README.md"
        dependencies = ["tai42-contract>=1.1,<2"]
        """,
    )
    stale_yaml = """
        spec_version: 1
        namespace: acme
        name: demo
        version: 1.0.0
        contract: '>=1.1,<2'
        """
    _write(root / "plugins/demo/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/demo/src/tai42_demo/tai-plugin.yml", stale_yaml)
    _write(root / "plugins/demo/README.md", _STATEMENT)
    _write(
        root / "plugins/other/pyproject.toml",
        """
        [project]
        name = "tai42-other"
        version = "1.0.0"
        readme = "README.md"
        dependencies = ["tai42-contract>=2.0,<3"]
        """,
    )
    _write(root / "plugins/other/README.md", _STATEMENT_SYNCED)


_PENDING_PYPROJECT = "plugins/demo/pyproject.toml"
_PENDING_DESCRIPTORS = ("plugins/demo/tai-plugin.yml", "plugins/demo/src/tai42_demo/tai-plugin.yml")
_PENDING_README = "plugins/demo/README.md"


def test_pending_writes_tree_drifts_on_every_surface(tmp_path: Path):
    """The fixture the write-nothing test relies on: a specifier, both descriptor
    pins and a README statement are all pending at once, so all three write paths
    are exercised there."""
    _build_pending_writes_tree(tmp_path)
    report = range_sync.check(tmp_path)

    assert [f"{member}/pyproject.toml" for member, _ in report.spec_changes] == [_PENDING_PYPROJECT]
    assert {path for path, _, _ in report.contract_changes} == set(_PENDING_DESCRIPTORS)
    assert {path for path, _, _ in report.readme_changes} == {_PENDING_README}


def test_apply_writes_nothing_when_a_readme_is_refused(tmp_path: Path):
    """A refused README aborts the whole apply before ANY file is written: the
    pyproject specifier, both descriptor pins and the README statement that were
    all pending stay byte-for-byte as they were."""
    _build_pending_writes_tree(tmp_path)
    poisoned = tmp_path / "plugins/other/README.md"
    poisoned.write_text(poisoned.read_text() + "\nPin tai42-contract>=1.1,<2 by hand.\n")
    pending = (_PENDING_PYPROJECT, *_PENDING_DESCRIPTORS, _PENDING_README)
    before = {rel: (tmp_path / rel).read_bytes() for rel in pending}
    before_tree = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    with pytest.raises(RuntimeError, match=re.escape("plugins/other/README.md")):
        range_sync.apply(tmp_path)

    assert (tmp_path / _PENDING_PYPROJECT).read_bytes() == before[_PENDING_PYPROJECT], "pyproject specifier written"
    for rel in _PENDING_DESCRIPTORS:
        assert (tmp_path / rel).read_bytes() == before[rel], f"descriptor pin written: {rel}"
    assert (tmp_path / _PENDING_README).read_bytes() == before[_PENDING_README], "README statement written"
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before_tree


def test_declared_readme_missing_raises(tmp_path: Path):
    _build_readme_tree(tmp_path)
    (tmp_path / "plugins/demo/README.md").unlink()
    with pytest.raises(RuntimeError, match=r"\[project\].readme points at a missing file"):
        range_sync.check(tmp_path)
