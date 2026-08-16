"""Unit tests for scripts/range_sync.py — the deterministic derive-and-rewrite
of first-party version ranges. Hermetic: every test builds a small sample tree
under ``tmp_path`` and never asserts against the real repo layout."""

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
