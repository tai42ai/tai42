#!/usr/bin/env python3
"""Release API-diff gate: fail a publish whose public-API change class exceeds
the version bump the release-please label produced.

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
required parameters added, parameter kinds/types narrowed, and so on) plus the
removal of a whole shipped top-level module. A module that is new at the tag is
additive and skipped. A doc-only change to a pydantic ``Field(...)`` — editing
only ``description``/``title``/``examples`` — is documentation metadata, not API
surface, so it is not a breaking change. Any tooling failure — an unreadable
config, a mode the config does not name, a module that cannot be loaded — raises
loudly; the gate never passes a release on a classification it could not compute.

CLI::

    api_gate.py --package tai42-kit --dir core/kit --version 1.8.0
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import NoReturn

import yaml

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_MODES = ("label-honesty", "strict")
_SKIP_DIRS = {"__pycache__"}
# pydantic ``Field(...)`` keywords that carry documentation only; a change to any
# of these is metadata, not API surface.
_FIELD_DOC_KEYWORDS = frozenset({"description", "title", "examples"})


def _fail(message: str) -> NoReturn:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def _version_class(new_major: int) -> str:
    return ">=1.0" if new_major >= 1 else "0.x"


def _allowed_bumps(mode: str, new_major: int) -> frozenset[str]:
    """Bump classes that may carry a breaking surface change, given the mode and
    the NEW version's major component. Strict allows only a major for every
    version; label-honesty allows a major for a ``>=1.0`` package and both a
    minor and a major for a ``0.x`` one (the 0.x breaking slot)."""
    if mode == "strict":
        return frozenset({"major"})
    if mode == "label-honesty":
        return frozenset({"major"}) if new_major >= 1 else frozenset({"minor", "major"})
    _fail(f"api-gate config mode {mode!r} not one of {_MODES}")


def _read_mode(repo_root: Path) -> str:
    config_path = repo_root / ".github" / "api-gate.yml"
    if not config_path.is_file():
        _fail(f"api-gate config not found at {config_path}")
    mode = yaml.safe_load(config_path.read_text()).get("mode")
    if mode not in _MODES:
        _fail(f"api-gate config mode {mode!r} not one of {_MODES}")
    return mode


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


def _previous_tag(package: str, new_version: str, repo_root: Path) -> str | None:
    """Highest ``<package>-v<version>`` tag strictly below ``new_version``."""
    prefix = f"{package}-v"
    out = subprocess.run(
        ["git", "tag", "--list", f"{prefix}*"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    new_key = _parse_version(new_version)
    candidates: list[tuple[tuple[int, int, int], str]] = []
    for tag in out.split():
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
    out = subprocess.run(
        ["git", "ls-tree", ref, f"{src_rel}/"],
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


def _field_call_name(func: ast.expr) -> str | None:
    """Callee name of a call node when it is a bare ``Field`` name or a dotted
    attribute ending in ``.Field`` (``pydantic.Field``), else ``None``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_doc_only_field_change(old_expr: str, new_expr: str) -> bool:
    """True only when both value expressions parse as ``Field(...)`` calls that
    are structurally equal after dropping the documentation-only keywords
    (``description``/``title``/``examples``). A parse failure or a non-``Field``
    expression returns ``False`` — the classification stays breaking — and any
    other surprise propagates to the caller as a loud crash."""
    try:
        old_node = ast.parse(old_expr, mode="eval").body
        new_node = ast.parse(new_expr, mode="eval").body
    except (SyntaxError, ValueError):
        return False
    if not (isinstance(old_node, ast.Call) and isinstance(new_node, ast.Call)):
        return False
    if _field_call_name(old_node.func) != "Field":
        return False
    if _field_call_name(new_node.func) != "Field":
        return False
    old_node.keywords = [kw for kw in old_node.keywords if kw.arg not in _FIELD_DOC_KEYWORDS]
    new_node.keywords = [kw for kw in new_node.keywords if kw.arg not in _FIELD_DOC_KEYWORDS]
    return ast.dump(old_node) == ast.dump(new_node)


# Collection wrappers whose single positional argument holds the underlying
# literal, so ``MappingProxyType({...})`` and ``frozenset({...})`` compare as the
# dict/set they wrap. A change of wrapper (``frozenset`` -> ``set``) is NOT a
# match: both sides must unwrap through an identical wrapper-name chain.
_COLLECTION_WRAPPERS = frozenset({"MappingProxyType", "frozenset", "tuple", "list", "set", "dict"})


def _unwrap_collection(node: ast.expr) -> tuple[ast.expr, tuple[str, ...]]:
    """Peel any chain of recognised single-argument collection wrappers off
    ``node``, returning the innermost expression and the wrapper-name chain that
    was peeled (outermost first). A wrapper only unwraps when its callee is a
    bare or dotted name in :data:`_COLLECTION_WRAPPERS` with EXACTLY one
    positional argument and NO keywords; anything else stops the peeling."""
    chain: list[str] = []
    while (
        isinstance(node, ast.Call)
        and _field_call_name(node.func) in _COLLECTION_WRAPPERS
        and len(node.args) == 1
        and not node.keywords
        and not isinstance(node.args[0], ast.Starred)
    ):
        name = _field_call_name(node.func)
        assert name is not None  # guarded by the membership test above
        chain.append(name)
        node = node.args[0]
    return node, tuple(chain)


def _is_subsequence(old: list[str], new: list[str]) -> bool:
    """True when every element of ``old`` appears in ``new`` in the same relative
    order (insertions anywhere are allowed, reorderings are not)."""
    it = iter(new)
    return all(elem in it for elem in old)


def _is_additive_collection_growth(old_expr: str, new_expr: str) -> bool:
    """True only when both value expressions are same-typed collection constants
    and the new one is a STRICT SUPERSET of the old — a purely additive growth
    (dict/set gained entries, tuple/list gained elements without disturbing the
    order of the pre-existing ones). Every other relation — element removal or
    edit, a changed value for a preserved dict key, a reorder, a wrapper-type or
    collection-type change, a duplicate dict key, a non-collection expression, or
    an identical collection — returns ``False`` so the classification stays
    breaking. The comparison is purely structural (parse-only, never evaluated);
    a parse failure or any ambiguity fails closed to ``False``."""
    try:
        old_node = ast.parse(old_expr, mode="eval").body
        new_node = ast.parse(new_expr, mode="eval").body
    except (SyntaxError, ValueError):
        return False
    old_inner, old_chain = _unwrap_collection(old_node)
    new_inner, new_chain = _unwrap_collection(new_node)
    if old_chain != new_chain:
        return False
    if type(old_inner) is not type(new_inner):
        return False
    if isinstance(old_inner, ast.Dict) and isinstance(new_inner, ast.Dict):
        old_pairs = [
            (ast.dump(k), ast.dump(v)) for k, v in zip(old_inner.keys, old_inner.values, strict=True) if k is not None
        ]
        new_pairs = [
            (ast.dump(k), ast.dump(v)) for k, v in zip(new_inner.keys, new_inner.values, strict=True) if k is not None
        ]
        if len(old_pairs) != len(old_inner.keys) or len(new_pairs) != len(new_inner.keys):
            return False  # a ``**expansion`` has a None key node — not a plain literal
        old_keys = [k for k, _ in old_pairs]
        new_keys = [k for k, _ in new_pairs]
        if len(set(old_keys)) != len(old_keys) or len(set(new_keys)) != len(new_keys):
            return False  # a duplicate key dump makes the mapping ambiguous
        new_by_key = dict(new_pairs)
        for key, value in old_pairs:
            if new_by_key.get(key) != value:
                return False  # a missing key or a changed value for a preserved key
        return len(new_pairs) > len(old_pairs)
    if isinstance(old_inner, ast.Set) and isinstance(new_inner, ast.Set):
        old_dumps = [ast.dump(e) for e in old_inner.elts]
        new_dumps = [ast.dump(e) for e in new_inner.elts]
        if any(isinstance(e, ast.Starred) for e in (*old_inner.elts, *new_inner.elts)):
            return False
        return Counter(old_dumps) <= Counter(new_dumps) and len(new_dumps) > len(old_dumps)
    if isinstance(old_inner, ast.Tuple | ast.List) and isinstance(new_inner, ast.Tuple | ast.List):
        if any(isinstance(e, ast.Starred) for e in (*old_inner.elts, *new_inner.elts)):
            return False
        old_dumps = [ast.dump(e) for e in old_inner.elts]
        new_dumps = [ast.dump(e) for e in new_inner.elts]
        return len(new_dumps) > len(old_dumps) and _is_subsequence(old_dumps, new_dumps)
    return False


def _breakages(module: str, ref: str, search: str) -> list[str]:
    # griffe is the heavy release-only dependency (api-gate group); import it
    # lazily so the pure decision helpers can be imported and unit-tested in an
    # environment that does not carry it.
    import griffe

    old = griffe.load_git(module, ref=ref, search_paths=[search])
    new = griffe.load(module, search_paths=[search])
    explained: list[str] = []
    for b in griffe.find_breaking_changes(old, new):
        if b.kind is griffe.BreakageKind.ATTRIBUTE_CHANGED_VALUE and (
            _is_doc_only_field_change(str(b.old_value), str(b.new_value))
            or _is_additive_collection_growth(str(b.old_value), str(b.new_value))
        ):
            continue
        explained.append(_ANSI.sub("", b.explain(style=griffe.ExplanationStyle.ONE_LINE)))
    return explained


def _gate_passes(mode: str, old_version: str, new_version: str, has_breaking: bool) -> tuple[bool, str]:
    """The gate's pass/fail decision, factored out of the git/griffe I/O so the
    whole mode x version x bump matrix is unit-testable without those deps.

    Returns ``(passes, reason)``; the reason names mode, the version class, the
    computed allowed bump set and the actual bump, so a failure explains itself.
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", required=True, help="release-please package-name")
    parser.add_argument("--dir", required=True, help="member dir under the repo root")
    parser.add_argument("--version", required=True, help="version being released")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    mode = _read_mode(repo_root)
    src_rel = f"{args.dir}/src"
    src = repo_root / src_rel

    previous = _previous_tag(args.package, args.version, repo_root)
    if previous is None:
        print(f"{args.package}: first release, no prior tag to diff — gate passes.")
        return
    old_version = previous[len(f"{args.package}-v") :]
    bump = _bump_class(old_version, args.version)

    new_modules = _top_modules_worktree(src)
    old_modules = _top_modules_ref(previous, src_rel, repo_root)

    findings: list[str] = []
    for module in sorted(old_modules - new_modules):
        findings.append(f"{module}: shipped top-level module was removed")
    for module in sorted(old_modules & new_modules):
        findings.extend(_breakages(module, previous, src_rel))

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
