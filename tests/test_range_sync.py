"""Unit tests for scripts/range_sync.py — the deterministic derive-and-rewrite
of first-party version ranges. Hermetic: every test builds a small sample tree
under ``tmp_path`` — the sole exception is the real-workspace inertness guard,
which proves the cross-major pin feature adds no warnings, no preservation, and
no drift to the live fleet."""

from __future__ import annotations

import tomllib
from pathlib import Path
from textwrap import dedent

import pytest

import range_sync  # importable via the scripts/ path tests/conftest.py injects

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


def test_real_workspace_pin_feature_is_inert():
    """The live fleet is synced and carries no pins: the feature must add no
    warnings, no preservation, no untouched-descriptor markers, and no drift
    (zero-diff proof)."""
    report = range_sync.check(range_sync._repo_root())
    assert report.warnings == []
    assert report.preserved == []
    assert report.descriptor_untouched == []
    assert report.dirty is False


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


# ------------------------------------------------- apply-mode stdout (B1)


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
