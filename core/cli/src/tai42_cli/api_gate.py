#!/usr/bin/env python3
"""Release API-diff gate: fail a publish whose public-API change class exceeds its version bump.

The gate compares a to-be-released package's public API at the pushed tag
against its previous released tag with ``griffe`` and classifies the result as
breaking or not. It reads the single mode switch in ``.github/api-gate.yml``:

  * label-honesty — a breaking surface change may ship only in the bump an
    honest release-please label would have produced, which depends on the NEW
    version's major: a major bump for a ``>=1.0`` package, or a minor bump for a
    ``0.x`` one (release-please maps an honest breaking change on 0.x to a minor,
    and semver's 0.x convention treats the minor as the breaking slot). A patch
    never carries a breaking change.
  * strict — a breaking surface change may ship only in a major bump, for every
    version: a ``0.x`` package must graduate to ``1.0.0`` to break once strict is
    on.

The allowed bump set is therefore a function of mode AND the new version's
major component — never version-blind — so a ``>=1.0`` package cannot slip a
breaking change through a mislabelled minor.

Breaking changes are ``griffe``'s own classification (public objects removed,
required parameters added, parameter kinds/types narrowed, base classes removed,
and so on), plus a public attribute whose bound value expression changed, plus the
removal of a whole shipped top-level module. The gate compares attribute values
itself, walking the same public-member tree ``griffe`` walks (inherited members
included, each object once at its canonical path), because a shipped default or
constant is part of the published surface: a consumer receives the new value.
A module that is new at the tag is additive and skipped. A module attribute bound
to a ``logging.getLogger(...)`` call is the module's own log channel, never API
surface: its removal or rebinding is not a breaking change. A collection constant
that only gains entries, and a ``Literal[...]`` alias that only gains members, are
additive and not breaking. A doc-only change to a known decorator/constructor call —
a pydantic ``Field(...)`` or a ``typer`` ``Typer``/``Option``/``Argument`` —
editing only that callee's documentation keywords (``help``/``description``/
``title``/``examples`` and kin) is documentation metadata, not API surface, so it
is not a breaking change. A ``Field`` ``json_schema_extra`` mapping is forgiven only
key by key: both sides must be literal string-keyed dicts (an absent mapping counts
as empty) and every key whose value is added, removed or changed must be a known
documentation key (``x-tai42-expression``/``description``/``title``/``examples``/
``deprecated``). A behaviour-bearing key (``secret``, ``reload``, ``key_material``,
``type`` — anything off that allowlist), a callable or otherwise non-literal
mapping, keeps gating; the same key-level check governs wrapping a bare-literal
default into ``Field(default=<same>, …)``. Any tooling failure — an unreadable
config, a mode the config does not name, a module that cannot be loaded — raises
loudly; the gate never passes a release on a classification it could not compute.

The tool is the ``tai42-api-gate`` console script (the ``api-gate`` extra of
``tai42-cli`` carries its ``griffe`` and ``pyyaml`` dependencies). It runs against
any repo laid out as this workspace is — ``--repo-root`` locates the tree (default:
the working directory) and ``--config`` the mode file (default
``<repo-root>/.github/api-gate.yml``)::

    tai42-api-gate --package tai42-kit --dir core/kit --version 1.8.0
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import NoReturn

import yaml

from tai42_cli.api_gate_values import is_logger_binding, value_change_findings

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MODES = ("label-honesty", "strict")
_SKIP_DIRS = {"__pycache__"}


def _fail(message: str) -> NoReturn:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def _version_class(new_major: int) -> str:
    return ">=1.0" if new_major >= 1 else "0.x"


def _allowed_bumps(mode: str, new_major: int) -> frozenset[str]:
    """Bump classes that may carry a breaking surface change, given mode and new-version major.

    Strict allows only a major for every version; label-honesty allows a major for a
    ``>=1.0`` package and both a minor and a major for a ``0.x`` one (the 0.x breaking slot).
    """
    if mode == "strict":
        return frozenset({"major"})
    if mode == "label-honesty":
        return frozenset({"major"}) if new_major >= 1 else frozenset({"minor", "major"})
    _fail(f"api-gate config mode {mode!r} not one of {_MODES}")


def _read_mode(config_path: Path) -> str:
    if not config_path.is_file():
        _fail(f"api-gate config not found at {config_path}")
    mode = yaml.safe_load(config_path.read_text()).get("mode")
    if mode not in _MODES:
        _fail(f"api-gate config mode {mode!r} not one of {_MODES}")
    return mode


def _read_voided_tags(config_path: Path) -> dict[str, str]:
    """The ``voided_tags`` record from the gate config, mapped tag -> one-line reason.

    A voided tag is one whose release job failed before publishing anything, so the
    gate never baselines against it (nothing shipped at that version). An absent key
    yields the empty map; a non-list value or a malformed entry raises loudly.
    """
    if not config_path.is_file():
        _fail(f"api-gate config not found at {config_path}")
    raw = yaml.safe_load(config_path.read_text()).get("voided_tags", [])
    if not isinstance(raw, list):
        _fail("api-gate config 'voided_tags' must be a list of {tag, reason} entries")
    voided: dict[str, str] = {}
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) != {"tag", "reason"}:
            _fail(f"api-gate 'voided_tags' entry {entry!r} must be a mapping with exactly 'tag' and 'reason'")
        tag, reason = entry["tag"], entry["reason"]
        if not isinstance(tag, str) or not tag or not isinstance(reason, str) or not reason:
            _fail(f"api-gate 'voided_tags' entry {entry!r} must give a non-empty string tag and reason")
        if tag in voided:
            _fail(f"api-gate 'voided_tags' names tag {tag!r} more than once")
        voided[tag] = reason
    return voided


def _all_tags(repo_root: Path) -> set[str]:
    out = subprocess.run(
        ["git", "tag", "--list"],  # noqa: S607 fixed, trusted executable resolved from PATH
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return set(out.split())


def _voided_tags(config_path: Path, repo_root: Path) -> dict[str, str]:
    """Read the voided-tag record and assert every tag it names still exists in the repo.

    A voided entry pointing at a tag that is not present is a stale or typo'd record —
    a config error — and fails the gate loudly rather than being ignored.
    """
    voided = _read_voided_tags(config_path)
    if voided:
        missing = sorted(set(voided) - _all_tags(repo_root))
        if missing:
            _fail(f"api-gate 'voided_tags' names tag(s) not present in the repo: {', '.join(missing)}")
    return voided


def _parse_version(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        _fail(f"version {version!r} is not a bare major.minor.patch")
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _bump_class(old: str, new: str) -> str:
    o_major, o_minor, o_patch = _parse_version(old)
    n_major, n_minor, n_patch = _parse_version(new)
    if (n_major, n_minor, n_patch) <= (o_major, o_minor, o_patch):
        _fail(f"new version {new} does not increase previous released {old}")
    if n_major != o_major:
        return "major"
    if n_minor != o_minor:
        return "minor"
    return "patch"


def _previous_tag(package: str, new_version: str, repo_root: Path, *, config_path: Path | None = None) -> str | None:
    """Highest ``<package>-v<version>`` tag strictly below ``new_version``, skipping voided ones.

    A voided tag is one whose release job failed before it published anything, so it is
    not a baseline: comparing a later release against it would measure a surface nobody
    ever received.

    The voided set is sourced here from the api-gate config (``config_path``, default
    ``<repo_root>/.github/api-gate.yml``) so every caller is covered without threading it
    through: a missing config means no voids (the gate runs against trees that carry no
    api-gate config), while a present-but-malformed one, or a void naming an absent tag,
    raises loudly. The config is read only when it exists, and the stale-void git check
    runs only when voids are declared, so a tree with no voids pays for no extra git call.
    """
    if config_path is None:
        config_path = repo_root / ".github" / "api-gate.yml"
    voided = frozenset(_voided_tags(config_path, repo_root)) if config_path.is_file() else frozenset()
    prefix = f"{package}-v"
    out = subprocess.run(  # noqa: S603 fixed, trusted argv; no shell and no user input
        ["git", "tag", "--list", f"{prefix}*"],  # noqa: S607 fixed, trusted executable resolved from PATH
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    new_key = _parse_version(new_version)
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for tag in out.split():
        if tag in voided:
            continue
        version = tag[len(prefix) :]
        if re.fullmatch(r"\d+\.\d+\.\d+", version) and _parse_version(version) < new_key:
            candidates.append((_parse_version(version), tag))
    if not candidates:
        return None
    return max(candidates)[1]


# A top-level module is either a package DIR or a single ``*.py`` FILE directly
# under ``src`` — both are enumerated by their import name (the dir name, or the
# file stem). ``__init__.py``, the skip dirs and ``*.egg-info`` are excluded, and
# non-``.py`` files (``py.typed`` and friends) are not modules. The worktree and
# git-ref sides must stay symmetric or a module present on one side alone reads
# as a spurious add/removal.
def _is_module_dir(name: str) -> bool:
    return name not in _SKIP_DIRS and not name.endswith(".egg-info")


def _module_name_of_file(name: str) -> str | None:
    if name.endswith(".py") and name != "__init__.py":
        return name[: -len(".py")]
    return None


def _top_modules_worktree(src: Path) -> set[str]:
    if not src.is_dir():
        _fail(f"package src dir not found at {src}")
    modules: set[str] = set()
    for p in src.iterdir():
        if p.is_dir():
            if _is_module_dir(p.name):
                modules.add(p.name)
        else:
            stem = _module_name_of_file(p.name)
            if stem is not None:
                modules.add(stem)
    return modules


def _top_modules_ref(ref: str, src_rel: str, repo_root: Path) -> set[str]:
    out = subprocess.run(  # noqa: S603 fixed, trusted argv; no shell and no user input
        ["git", "ls-tree", ref, f"{src_rel}/"],  # noqa: S607 fixed, trusted executable resolved from PATH
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    modules: set[str] = set()
    for line in out.splitlines():
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        obj_type = meta.split()[1]
        name = Path(path).name
        if obj_type == "tree":
            if _is_module_dir(name):
                modules.add(name)
        elif obj_type == "blob":
            stem = _module_name_of_file(name)
            if stem is not None:
                modules.add(stem)
    return modules


_DIST_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalized_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_pyproject(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text())
    except FileNotFoundError:
        _fail(f"pyproject.toml not found at {path}")


def _project_dependencies(member_dir: Path) -> list[str]:
    """The normalized distribution names of a member's ``[project] dependencies``."""
    names: list[str] = []
    for requirement in _read_pyproject(member_dir / "pyproject.toml").get("project", {}).get("dependencies", []):
        match = _DIST_NAME.match(requirement)
        if match is None:
            _fail(f"unparseable dependency {requirement!r} in {member_dir / 'pyproject.toml'}")
        names.append(_normalized_dist_name(match.group(1)))
    return names


def _workspace_members(repo_root: Path) -> dict[str, Path]:
    """Normalized distribution name -> member dir, for every ``[tool.uv.workspace]`` member of the repo root.

    A repo root without a ``pyproject.toml`` or without a workspace has no members. A member dir
    without a ``pyproject.toml`` (a descriptor-only dir) carries no distribution.
    """
    root_pyproject = repo_root / "pyproject.toml"
    if not root_pyproject.is_file():
        return {}
    workspace = _read_pyproject(root_pyproject).get("tool", {}).get("uv", {}).get("workspace", {})
    excluded = {path for pattern in workspace.get("exclude", []) for path in repo_root.glob(pattern)}
    members: dict[str, Path] = {}
    for pattern in workspace.get("members", []):
        for member in sorted(repo_root.glob(pattern)):
            if member in excluded or not (member / "pyproject.toml").is_file():
                continue
            name = _read_pyproject(member / "pyproject.toml").get("project", {}).get("name")
            if name is not None:
                members[_normalized_dist_name(name)] = member
    return members


def _first_party_dependencies(package_dir: Path, repo_root: Path) -> list[tuple[str, str]]:
    """The package's first-party dependencies as ``(distribution name, src dir relative to the repo root)``.

    A first-party dependency is a ``[project] dependencies`` entry whose distribution is a workspace
    member, followed transitively (a first-party base may itself inherit from another member's
    class). Sorted by distribution name.
    """
    members = _workspace_members(repo_root)
    found: dict[str, Path] = {}
    pending = _project_dependencies(package_dir)
    while pending:
        name = pending.pop()
        member = members.get(name)
        if member is None or name in found:
            continue
        found[name] = member
        pending.extend(_project_dependencies(member))
    return sorted((name, (member / "src").relative_to(repo_root).as_posix()) for name, member in found.items())


def _breakages(module: str, ref: str, src_rel: str, repo_root: Path, first_party: list[str]) -> list[str]:
    # griffe is the heavy release-only dependency (api-gate extra); import it
    # lazily so the pure decision helpers can be imported and unit-tested in an
    # environment that does not carry it.
    import griffe

    # The ref side loads from a worktree griffe checks out of ``repo_root`` (a
    # repo-relative search path); the worktree side loads the current tree at the
    # same path made absolute, so both sides are anchored to ``repo_root`` and
    # never to the process's working directory. Each side first loads the
    # first-party dependencies (``first_party``: their src dirs) into its own
    # collection, so a class inheriting from a first-party base resolves its
    # inherited members on both sides; a third-party base stays unresolved.
    old_collection = griffe.ModulesCollection()
    new_collection = griffe.ModulesCollection()
    for dep_src_rel in first_party:
        for dep in sorted(_top_modules_ref(ref, dep_src_rel, repo_root)):
            griffe.load_git(dep, ref=ref, repo=repo_root, search_paths=[dep_src_rel], modules_collection=old_collection)
        for dep in sorted(_top_modules_worktree(repo_root / dep_src_rel)):
            griffe.load(dep, search_paths=[repo_root / dep_src_rel], modules_collection=new_collection)
    old = griffe.load_git(module, ref=ref, repo=repo_root, search_paths=[src_rel], modules_collection=old_collection)
    new = griffe.load(module, search_paths=[repo_root / src_rel], modules_collection=new_collection)
    explained: list[str] = []
    for b in griffe.find_breaking_changes(old, new):
        if isinstance(b.obj, griffe.Attribute) and is_logger_binding(str(b.obj.value)):
            continue
        explained.append(_ANSI.sub("", b.explain(style=griffe.ExplanationStyle.ONE_LINE)))
    explained.extend(value_change_findings(old, new))
    return explained


def _gate_passes(mode: str, old_version: str, new_version: str, has_breaking: bool) -> tuple[bool, str]:
    """The gate's pass/fail decision, factored out of the git/griffe I/O for unit testing.

    Factoring it out keeps the whole mode x version x bump matrix testable without those deps.
    Returns ``(passes, reason)``; the reason names mode, the version class, the computed
    allowed bump set and the actual bump, so a failure explains itself.
    """
    bump = _bump_class(old_version, new_version)
    new_major = _parse_version(new_version)[0]
    allowed = _allowed_bumps(mode, new_major)
    if not has_breaking:
        return True, f"no breaking surface changes for a {bump} bump"
    detail = (
        f"mode={mode}, version class={_version_class(new_major)}, "
        f"computed allowed bump(s)={','.join(sorted(allowed))}, bump={bump}"
    )
    if bump in allowed:
        return True, f"breaking changes are honest ({detail})"
    return False, f"is a {bump} bump but carries breaking surface changes ({detail})"


def main() -> None:
    """Parse CLI arguments, run the gate, and exit non-zero when the release is dishonest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, help="release-please package-name")
    parser.add_argument("--dir", required=True, help="member dir under the repo root")
    parser.add_argument("--version", required=True, help="version being released")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="root of the tree to gate (default: the working directory)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="api-gate mode file (default: <repo-root>/.github/api-gate.yml)",
    )
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    config_path = args.config if args.config is not None else repo_root / ".github" / "api-gate.yml"
    mode = _read_mode(config_path)
    src_rel = f"{args.dir}/src"
    src = repo_root / src_rel

    previous = _previous_tag(args.package, args.version, repo_root, config_path=config_path)
    if previous is None:
        print(f"{args.package}: first release, no prior tag to diff — gate passes.")
        return
    old_version = previous[len(f"{args.package}-v") :]
    bump = _bump_class(old_version, args.version)

    new_modules = _top_modules_worktree(src)
    old_modules = _top_modules_ref(previous, src_rel, repo_root)

    findings: list[str] = [
        f"{module}: shipped top-level module was removed" for module in sorted(old_modules - new_modules)
    ]
    first_party = [dep_src_rel for _name, dep_src_rel in _first_party_dependencies(repo_root / args.dir, repo_root)]
    for module in sorted(old_modules & new_modules):
        findings.extend(_breakages(module, previous, src_rel, repo_root, first_party))

    header = f"{args.package}: {old_version} -> {args.version} ({bump} bump, mode={mode})"
    passes, reason = _gate_passes(mode, old_version, args.version, bool(findings))
    if not findings:
        print(f"{header}: {reason} — gate passes.")
        return

    print(f"{header}: {len(findings)} breaking surface item(s):")
    for line in findings:
        print(f"  - {line}")
    if passes:
        print(f"{header}: {reason} — gate passes.")
        return
    _fail(f"{args.package} {args.version} {reason}. Offending items: {'; '.join(findings)}")


if __name__ == "__main__":
    main()
