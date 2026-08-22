"""Unit tests for scripts/api_gate.py — the release API-diff gate. Hermetic:
the pure decision helpers are tested directly, the git-backed helpers against a
throwaway repo built under ``tmp_path``, and the full griffe end-to-end run is
guarded by ``importorskip`` (griffe is the release-only ``api-gate`` group, absent
from the fleet test environment). No network and no real PyPI or tag are touched."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import api_gate  # importable via the scripts/ path tests/conftest.py injects

# ----------------------------------------------------------------- version parse


@pytest.mark.parametrize(
    ("version", "expected"),
    [("1.8.0", (1, 8, 0)), ("0.3.1", (0, 3, 1)), ("2.0.0", (2, 0, 0))],
)
def test_parse_version(version: str, expected: tuple[int, int, int]):
    assert api_gate._parse_version(version) == expected


def test_parse_version_rejects_non_semver():
    with pytest.raises(SystemExit):
        api_gate._parse_version("1.2")


# --------------------------------------------------------------------- bump class


@pytest.mark.parametrize(
    ("old", "new", "expected"),
    [
        ("1.0.0", "2.0.0", "major"),
        ("1.0.0", "1.1.0", "minor"),
        ("1.0.0", "1.0.1", "patch"),
        ("0.1.0", "0.2.0", "minor"),
        ("0.1.0", "1.0.0", "major"),
    ],
)
def test_bump_class(old: str, new: str, expected: str):
    assert api_gate._bump_class(old, new) == expected


@pytest.mark.parametrize(("old", "new"), [("1.1.0", "1.0.0"), ("1.0.0", "1.0.0")])
def test_bump_class_non_increasing_fails_loudly(old: str, new: str):
    with pytest.raises(SystemExit):
        api_gate._bump_class(old, new)


# ------------------------------------------------------------------- allowed set


@pytest.mark.parametrize(
    ("mode", "new_major", "expected"),
    [
        # label-honesty is version-aware: a >=1.0 package may break only in a
        # major, a 0.x one may break in a minor (or major).
        ("label-honesty", 1, {"major"}),
        ("label-honesty", 2, {"major"}),
        ("label-honesty", 0, {"minor", "major"}),
        # strict is major-only for every version.
        ("strict", 0, {"major"}),
        ("strict", 1, {"major"}),
    ],
)
def test_allowed_bumps(mode: str, new_major: int, expected: set[str]):
    assert api_gate._allowed_bumps(mode, new_major) == frozenset(expected)


def test_allowed_bumps_unknown_mode_fails_loudly():
    with pytest.raises(SystemExit):
        api_gate._allowed_bumps("bogus", 1)


def test_version_class():
    assert api_gate._version_class(0) == "0.x"
    assert api_gate._version_class(1) == ">=1.0"
    assert api_gate._version_class(2) == ">=1.0"


# ----------------------------------------------------------- decision matrix glue
# The exact pass/fail table the gate exists to enforce, exercised through the
# real decision helper (no git, no griffe).


@pytest.mark.parametrize(
    ("mode", "old", "new", "has_breaking", "passes"),
    [
        # A >=1.0 package sneaking a breaking change into a minor is the incident
        # the gate must catch.
        ("label-honesty", "1.0.0", "1.1.0", True, False),
        # A 0.x package's breaking change is honest as a minor.
        ("label-honesty", "0.1.0", "0.2.0", True, True),
        # A >=1.0 package's breaking change is honest as a major.
        ("label-honesty", "1.0.0", "2.0.0", True, True),
        # strict never lets a 0.x break outside a major.
        ("strict", "0.1.0", "0.2.0", True, False),
        # strict lets a major carry it.
        ("strict", "1.0.0", "2.0.0", True, True),
        # No breaking surface change always passes, whatever the bump.
        ("label-honesty", "1.0.0", "1.0.1", False, True),
        ("strict", "0.1.0", "0.1.1", False, True),
    ],
)
def test_gate_decision_matrix(mode: str, old: str, new: str, has_breaking: bool, passes: bool):
    result, reason = api_gate._gate_passes(mode, old, new, has_breaking)
    assert result is passes
    if has_breaking and not passes:
        # A failure names mode, version class, computed class and bump.
        assert f"mode={mode}" in reason
        assert "version class=" in reason
        assert "computed allowed bump(s)=" in reason
        assert "bump=" in reason


def test_allowed_set_renders_comma_joined_sorted_no_brackets():
    # A 0.x package breaking in a patch: allowed={minor,major}, so the multi-element
    # rendering is exercised — pinned to the exact "major,minor" form (parity with
    # the mjs gate), never a bracketed/quoted list.
    _, reason = api_gate._gate_passes("label-honesty", "0.1.0", "0.1.1", True)
    assert "computed allowed bump(s)=major,minor, bump=patch" in reason
    assert "[" not in reason
    assert "'" not in reason


# ------------------------------------------------------ doc-only call filter
# A doc-only change to a known decorator/constructor call (``Field``, ``Typer``,
# ``Option``/``Argument``) confined to that callee's documentation keywords is
# metadata, not API surface — griffe flags it ATTRIBUTE_CHANGED_VALUE, the filter
# drops it. Every non-doc change stays breaking, and any unknown callee or
# unparseable expression keeps griffe's classification (breaking-by-default).


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # The real skeleton-2.1.0 incident: only the Field description changed.
        (
            "Field(default=None, gt=0, description='...old...')",
            "Field(default=None, gt=0, description='...new...')",
        ),
        # Title-only and examples-only edits are equally documentation.
        ("Field(default=1, title='A')", "Field(default=1, title='B')"),
        ("Field(default=1, examples=[1])", "Field(default=1, examples=[2])"),
        # A dotted ``pydantic.Field`` callee is recognised too.
        (
            "pydantic.Field(default=1, description='a')",
            "pydantic.Field(default=1, description='b')",
        ),
        # The tai42-cli 3.4.0 incident: only the Typer help text changed.
        (
            "typer.Typer(name='interactions', help='...old...', no_args_is_help=True)",
            "typer.Typer(name='interactions', help='...new...', no_args_is_help=True)",
        ),
        # A typer ``Option`` help-only edit is documentation.
        ("Option(None, '--flag', help='old')", "Option(None, '--flag', help='new')"),
    ],
)
def test_doc_only_call_change_is_not_breaking(old: str, new: str):
    assert api_gate._is_doc_only_call_change(old, new) is True


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # A default value change is real surface, never forgiven.
        ("Field(default=None, gt=0)", "Field(default=5, gt=0)"),
        # A constraint (gt) change is real surface.
        ("Field(default=None, gt=0)", "Field(default=None, gt=1)"),
        # Unknown-callee attribute value changes keep griffe's classification.
        ("some_call(a=1)", "some_call(a=2)"),
        ("42", "43"),
        # A description edit paired with a real default change is still breaking.
        (
            "Field(default=None, description='a')",
            "Field(default=5, description='b')",
        ),
        # An unparseable expression is a parse failure -> still breaking, no crash.
        ("Field(", "Field("),
        # A Typer help edit that ALSO flips a behaviour keyword is real surface.
        (
            "typer.Typer(name='interactions', help='old', no_args_is_help=True)",
            "typer.Typer(name='interactions', help='new', no_args_is_help=False)",
        ),
        # A Typer ``name`` change is real surface, not documentation.
        (
            "typer.Typer(name='old', help='same')",
            "typer.Typer(name='new', help='same')",
        ),
    ],
)
def test_non_doc_call_change_stays_breaking(old: str, new: str):
    assert api_gate._is_doc_only_call_change(old, new) is False


# ------------------------------------------------------------------- config read


def test_read_mode_valid(tmp_path: Path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "api-gate.yml").write_text("mode: strict\n")
    assert api_gate._read_mode(tmp_path) == "strict"


def test_read_mode_unknown_fails_loudly(tmp_path: Path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "api-gate.yml").write_text("mode: whatever\n")
    with pytest.raises(SystemExit):
        api_gate._read_mode(tmp_path)


def test_read_mode_missing_file_fails_loudly(tmp_path: Path):
    with pytest.raises(SystemExit):
        api_gate._read_mode(tmp_path)


# --------------------------------------------------------------- module discovery


def _make_src(src: Path) -> None:
    src.mkdir(parents=True)
    (src / "pkgmod").mkdir()
    (src / "pkgmod" / "__init__.py").write_text("x = 1\n")
    (src / "filemod.py").write_text("y = 2\n")  # single-file top module
    (src / "__init__.py").write_text("")  # excluded
    (src / "py.typed").write_text("")  # not a module
    (src / "helper.egg-info").mkdir()  # excluded
    (src / "__pycache__").mkdir()  # excluded


def test_top_modules_worktree_enumerates_dirs_and_files(tmp_path: Path):
    src = tmp_path / "src"
    _make_src(src)
    assert api_gate._top_modules_worktree(src) == {"pkgmod", "filemod"}


def test_top_modules_worktree_missing_dir_fails_loudly(tmp_path: Path):
    with pytest.raises(SystemExit):
        api_gate._top_modules_worktree(tmp_path / "nope")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.test")
    _git(repo, "config", "user.name", "Test")


def test_top_modules_ref_symmetric_with_worktree(tmp_path: Path):
    # A single-file top module present at the ref must be enumerated by the same
    # import name the worktree side yields — otherwise it reads as a spurious
    # removal and is never diffed.
    _init_repo(tmp_path)
    src = tmp_path / "core" / "widget" / "src"
    _make_src(src)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "tag", "widget-v0.1.0")

    at_ref = api_gate._top_modules_ref("widget-v0.1.0", "core/widget/src", tmp_path)
    at_worktree = api_gate._top_modules_worktree(src)
    assert at_ref == at_worktree == {"pkgmod", "filemod"}


def test_previous_tag(tmp_path: Path):
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("1")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "c")
    for v in ("0.1.0", "0.2.0", "1.0.0"):
        _git(tmp_path, "tag", f"widget-v{v}")
    assert api_gate._previous_tag("widget", "1.1.0", tmp_path) == "widget-v1.0.0"
    assert api_gate._previous_tag("widget", "0.2.0", tmp_path) == "widget-v0.1.0"
    assert api_gate._previous_tag("widget", "0.1.0", tmp_path) is None


# ---------------------------------------------------------- griffe end-to-end run
# Full main() over a synthetic git repo, exercising the module diff + griffe
# breakage classification + decision together. Skipped where griffe is absent.


def _run_main(
    monkeypatch: pytest.MonkeyPatch,
    repo: Path,
    *,
    package: str,
    member_dir: str,
    version: str,
) -> None:
    scripts = repo / "scripts"
    scripts.mkdir(exist_ok=True)
    monkeypatch.setattr(api_gate, "__file__", str(scripts / "api_gate.py"))
    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        "sys.argv",
        [
            "api_gate.py",
            "--package",
            package,
            "--dir",
            member_dir,
            "--version",
            version,
        ],
    )
    api_gate.main()


def _build_release_repo(repo: Path, *, mode: str, old_version: str, old_body: str, new_body: str) -> None:
    (repo / ".github").mkdir(parents=True)
    (repo / ".github" / "api-gate.yml").write_text(f"mode: {mode}\n")
    src = repo / "core" / "widget" / "src" / "widget"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text(old_body)
    _init_repo(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "release")
    _git(repo, "tag", f"widget-v{old_version}")
    (src / "__init__.py").write_text(new_body)


_KEEP = "def keep(a: int) -> int:\n    return a\n"
_REMOVE = "def gone(a: int) -> int:\n    return a\n"


@pytest.mark.parametrize(
    ("mode", "old", "new", "should_fail"),
    [
        ("label-honesty", "1.0.0", "1.1.0", True),  # breaking removal in a minor
        ("label-honesty", "0.1.0", "0.2.0", False),  # honest on 0.x
        ("label-honesty", "1.0.0", "2.0.0", False),  # honest on major
        ("strict", "0.1.0", "0.2.0", True),  # strict forbids 0.x minor break
    ],
)
def test_end_to_end_breaking_removal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mode: str,
    old: str,
    new: str,
    should_fail: bool,
):
    pytest.importorskip("griffe")
    _build_release_repo(
        tmp_path,
        mode=mode,
        old_version=old,
        old_body=_KEEP + _REMOVE,
        new_body=_KEEP,  # drops `gone` -> breaking
    )
    if should_fail:
        with pytest.raises(SystemExit):
            _run_main(
                monkeypatch,
                tmp_path,
                package="widget",
                member_dir="core/widget",
                version=new,
            )
    else:
        _run_main(
            monkeypatch,
            tmp_path,
            package="widget",
            member_dir="core/widget",
            version=new,
        )


def test_end_to_end_additive_change_passes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    pytest.importorskip("griffe")
    _build_release_repo(
        tmp_path,
        mode="strict",
        old_version="1.0.0",
        old_body=_KEEP,
        new_body=_KEEP + "def added(b: int) -> int:\n    return b\n",  # additive
    )
    # A pure addition is non-breaking, so even a patch passes under strict.
    _run_main(
        monkeypatch,
        tmp_path,
        package="widget",
        member_dir="core/widget",
        version="1.0.1",
    )


_FIELD_DESC_OLD = (
    "from pydantic import Field\n"
    "class TurnSettings:\n"
    "    timeout_seconds = Field(default=None, gt=0, description='...old...')\n"
)
_FIELD_DESC_NEW = (
    "from pydantic import Field\n"
    "class TurnSettings:\n"
    "    timeout_seconds = Field(default=None, gt=0, description='...new...')\n"
)
_FIELD_DEFAULT_NEW = (
    "from pydantic import Field\n"
    "class TurnSettings:\n"
    "    timeout_seconds = Field(default=5, gt=0, description='...old...')\n"
)


def test_end_to_end_field_description_only_passes_at_patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # The skeleton-2.1.0 incident end to end: only a Field description changed.
    # griffe flags ATTRIBUTE_CHANGED_VALUE, the filter drops it, so even a patch
    # under strict passes.
    pytest.importorskip("griffe")
    _build_release_repo(
        tmp_path,
        mode="strict",
        old_version="1.0.0",
        old_body=_FIELD_DESC_OLD,
        new_body=_FIELD_DESC_NEW,
    )
    _run_main(
        monkeypatch,
        tmp_path,
        package="widget",
        member_dir="core/widget",
        version="1.0.1",
    )


def test_end_to_end_field_default_change_stays_breaking_at_patch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    # A real Field default change is surface, so a patch under strict still fails.
    pytest.importorskip("griffe")
    _build_release_repo(
        tmp_path,
        mode="strict",
        old_version="1.0.0",
        old_body=_FIELD_DESC_OLD,
        new_body=_FIELD_DEFAULT_NEW,
    )
    with pytest.raises(SystemExit):
        _run_main(
            monkeypatch,
            tmp_path,
            package="widget",
            member_dir="core/widget",
            version="1.0.1",
        )
