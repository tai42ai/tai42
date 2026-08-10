#!/usr/bin/env python3
"""Derive first-party ``tai42-*`` version ranges from the released member
versions and rewrite them in place, so ranges never go stale by hand.

Two surfaces are kept in lockstep with the released versions:

  (A) first-party ``tai42-*`` dependency ranges in every workspace member's
      ``pyproject.toml`` (``[project].dependencies`` +
      ``[project.optional-dependencies]``);
  (B) the ``contract:`` pin in every ``tai-plugin.yml`` (both the root copy and
      the packaged ``src/.../tai-plugin.yml`` copy of each plugin), which tracks
      ``tai42-contract``.

The version source of truth is each member's ``[project].version``. The range
for a released version ``V`` is derived by a single rule (patch is ignored):

  * floor = ``<major>.<minor>`` always (minor precision; the released minor)
  * cap   = ``0.<minor+1>`` pre-1.0 (major == 0), else ``<major+1>`` from 1.0

e.g. 0.3.0 -> ``>=0.3,<0.4``; 0.5.1 -> ``>=0.5,<0.6``; 1.2.3 -> ``>=1.2,<2``.

Files are edited by targeted string/line replacement (not a toml/yaml
round-trip) so formatting and comments are preserved. Reading uses ``tomllib``.
Pure Python standard library only; no clock, network, or randomness.

CLI:
  python scripts/range_sync.py            # APPLY: rewrite in place (default)
  python scripts/range_sync.py --check    # verify sync; exit 1 + diff on drift
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

# The workspace member globs. Kept in sync with the root pyproject
# ``[tool.uv.workspace].members``; read from disk at runtime (below) so this
# constant is only the fallback default.
DEFAULT_WORKSPACE_GLOBS = ["core/*", "plugins/*", "e2e"]

# The first-party distribution prefix. Only requirements whose base name starts
# with this AND resolve to a known member version are rewritten.
FIRST_PARTY_PREFIX = "tai42-"

# The member whose derived range drives every ``contract:`` pin.
CONTRACT_PACKAGE = "tai42-contract"

# PEP 508-ish split: name, optional [extras], then the remainder (specifier +
# optional environment marker). We only ever rewrite the specifier portion.
_REQ_RE = re.compile(
    r"^\s*"
    r"(?P<name>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
    r"\s*(?P<extras>\[[^\]]*\])?"
    r"\s*(?P<rest>.*)$",
    re.DOTALL,
)

# A ``contract:`` line in a tai-plugin.yml, e.g. ``contract: '>=0.3,<0.4'``.
_CONTRACT_RE = re.compile(
    r"^(?P<indent>\s*)contract:\s*(?P<q>['\"])(?P<val>.*?)(?P=q)(?P<trail>\s*)$"
)


# --------------------------------------------------------------------------- #
# Core derivation                                                             #
# --------------------------------------------------------------------------- #

def derive_range(version: str) -> str:
    """Return the derived ``>=floor,<cap`` range for a released *version*.

    Patch level is ignored; the floor is always pinned to MINOR precision
    (``>=major.minor``) — a member claims compatibility only with the sibling
    minor it was built and tested against, so the floor never drops below the
    released minor. Only the cap depends on the 1.0 boundary: pre-1.0 the next
    minor is breaking (cap ``0.<minor+1>``); from 1.0 the next major is
    (cap ``<major+1>``).
    """
    v = version.strip().strip("\"'")
    # Drop any pre-release / local suffix; we only need the numeric release.
    core = re.split(r"[^0-9.]", v, maxsplit=1)[0]
    parts = core.split(".")
    major = int(parts[0])
    minor = int(parts[1]) if len(parts) > 1 and parts[1] != "" else 0
    # Floor is minor precision regardless of the major (no special-casing);
    # only the cap flips at the 1.0 breaking boundary.
    floor = f"{major}.{minor}"
    cap = f"0.{minor + 1}" if major == 0 else f"{major + 1}"
    return f">={floor},<{cap}"


@dataclass(frozen=True)
class ParsedRequirement:
    """A PEP 508 requirement split into the parts range-sync cares about."""

    name: str
    extras: str  # verbatim "[...]" including brackets, or "" if none
    specifier: str  # e.g. ">=0.2,<0.4" (stripped), or "" if version-less
    marker: str  # verbatim ";..." including the leading ';', or "" if none

    def with_specifier(self, new_spec: str) -> str:
        """Rebuild the requirement string with *new_spec* as the specifier,
        preserving name, extras, and environment marker verbatim."""
        return f"{self.name}{self.extras}{new_spec}{self.marker}"


def parse_requirement(raw: str) -> ParsedRequirement | None:
    """Split a requirement string. Returns None if it does not parse."""
    m = _REQ_RE.match(raw)
    if not m:
        return None
    rest = m.group("rest")
    if ";" in rest:
        idx = rest.index(";")
        specifier = rest[:idx].strip()
        marker = rest[idx:]  # keep ';' and everything after, verbatim
    else:
        specifier = rest.strip()
        marker = ""
    return ParsedRequirement(
        name=m.group("name"),
        extras=m.group("extras") or "",
        specifier=specifier,
        marker=marker,
    )


# --------------------------------------------------------------------------- #
# Discovery                                                                   #
# --------------------------------------------------------------------------- #

def _load_toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def workspace_globs(root: Path) -> list[str]:
    """Read the member globs from the root pyproject, falling back to the
    default if the table is missing."""
    try:
        data = _load_toml(root / "pyproject.toml")
        globs = data["tool"]["uv"]["workspace"]["members"]
        if isinstance(globs, list) and globs:
            return [str(g) for g in globs]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        pass
    return list(DEFAULT_WORKSPACE_GLOBS)


def discover_members(root: Path) -> list[Path]:
    """Return member directories (each containing a pyproject.toml), sorted and
    de-duplicated, discovered from the workspace globs."""
    seen: dict[str, Path] = {}
    for pattern in workspace_globs(root):
        for hit in sorted(root.glob(pattern)):
            if hit.is_dir() and (hit / "pyproject.toml").is_file():
                seen[hit.relative_to(root).as_posix()] = hit
    return [seen[k] for k in sorted(seen)]


def _normalize_name(name: str) -> str:
    """PEP 503 name normalization: lower-case and collapse any run of ``-``,
    ``_`` or ``.`` to a single ``-``. Matching on the normalized name means a
    dependency spelled non-canonically (``tai42_kit``, mixed case) still resolves
    to its member, so a stale range can never read as 'in sync' merely because of
    a spelling difference."""
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def first_party_versions(members: list[Path]) -> dict[str, str]:
    """Map NORMALIZED member distribution name -> current [project].version."""
    versions: dict[str, str] = {}
    for member in members:
        project = _load_toml(member / "pyproject.toml").get("project", {})
        name = project.get("name")
        version = project.get("version")
        if name and version:
            versions[_normalize_name(str(name))] = str(version)
    return versions


def _requirement_strings(pyproject: dict) -> list[str]:
    """Every requirement string in [project].dependencies and every
    [project.optional-dependencies] array — the tables scanned as change
    SOURCES. [tool.uv.sources] and [dependency-groups] are not scanned here.
    (Application is by locating the full quoted requirement literal in the file
    text — see ``_replace_quoted`` — so a first-party pin duplicated verbatim in
    another table is still brought in sync, which is the intended outcome.)"""
    project = pyproject.get("project", {})
    out: list[str] = list(project.get("dependencies", []) or [])
    for extra_deps in (project.get("optional-dependencies", {}) or {}).values():
        out.extend(extra_deps or [])
    return [r for r in out if isinstance(r, str)]


# --------------------------------------------------------------------------- #
# pyproject rewriting                                                         #
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SpecChange:
    """A single first-party specifier rewrite within one file."""

    dep_name: str
    old_req: str
    new_req: str


def compute_pyproject_changes(
    pyproject: dict, first_party: dict[str, str]
) -> list[SpecChange]:
    """Return the set of first-party specifier rewrites for one parsed
    pyproject. Version-less first-party refs and non-first-party refs are
    skipped. A change is emitted only when old != new."""
    changes: dict[str, SpecChange] = {}
    for raw in _requirement_strings(pyproject):
        parsed = parse_requirement(raw)
        if parsed is None:
            continue
        key = _normalize_name(parsed.name)
        if key not in first_party:
            continue
        if not parsed.specifier:
            continue  # version-less first-party ref: leave untouched
        derived = derive_range(first_party[key])
        if parsed.specifier == derived:
            continue
        new_req = parsed.with_specifier(derived)
        if new_req != raw:
            changes[raw] = SpecChange(parsed.name, raw, new_req)
    return list(changes.values())


def _replace_quoted(text: str, old: str, new: str) -> tuple[str, int]:
    """Replace the quoted literal ``old`` with ``new`` everywhere in *text*,
    preserving the quote style (single or double) used in the file. The match is
    the full quoted requirement literal (name+extras+specifier), which in a
    pyproject occurs only as a dependency entry — never in a comment — so a
    first-party pin repeated verbatim (e.g. under [dependency-groups]) is
    normalized too, keeping every first-party range in sync. Returns (text,
    count)."""
    for quote in ('"', "'"):
        literal = f"{quote}{old}{quote}"
        if literal in text:
            replacement = f"{quote}{new}{quote}"
            return text.replace(literal, replacement), text.count(literal)
    return text, 0


def rewrite_pyproject_text(text: str, changes: list[SpecChange]) -> tuple[str, int]:
    """Apply *changes* to the raw pyproject *text*. Returns (text, n_replaced).
    Raises if a change's literal is not found (guards silent no-ops)."""
    total = 0
    for change in changes:
        text, count = _replace_quoted(text, change.old_req, change.new_req)
        if count == 0:
            raise RuntimeError(
                f"could not locate requirement literal {change.old_req!r} to rewrite"
            )
        total += count
    return text, total


# --------------------------------------------------------------------------- #
# tai-plugin.yml rewriting                                                    #
# --------------------------------------------------------------------------- #

def rewrite_contract_yaml(text: str, new_range: str) -> tuple[str, bool]:
    """Rewrite the ``contract:`` line's quoted value to *new_range*, keeping the
    quote style. Returns (text, changed)."""
    changed = False
    out_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        newline = ""
        stripped = line
        if line.endswith("\r\n"):
            newline, stripped = "\r\n", line[:-2]
        elif line.endswith("\n"):
            newline, stripped = "\n", line[:-1]
        m = _CONTRACT_RE.match(stripped)
        if m and m.group("val") != new_range:
            q = m.group("q")
            rebuilt = f"{m.group('indent')}contract: {q}{new_range}{q}{m.group('trail')}"
            out_lines.append(rebuilt + newline)
            changed = True
        else:
            out_lines.append(line)
    return "".join(out_lines), changed


def contract_yaml_value(text: str) -> str | None:
    """Return the current ``contract:`` value, or None if absent."""
    for line in text.splitlines():
        m = _CONTRACT_RE.match(line)
        if m:
            return m.group("val")
    return None


def plugin_descriptor_files(members: list[Path], root: Path) -> list[Path]:
    """Every tai-plugin.yml (root + packaged copies) under each plugin member."""
    files: list[Path] = []
    for member in members:
        if member.relative_to(root).parts[0] != "plugins":
            continue
        files.extend(sorted(member.rglob("tai-plugin.yml")))
    return files


# --------------------------------------------------------------------------- #
# Apply / check                                                               #
# --------------------------------------------------------------------------- #

@dataclass
class SyncReport:
    """What an apply run changed / what a check run found out of sync."""

    spec_changes: list[tuple[str, SpecChange]]  # (member_path, change)
    contract_changes: list[tuple[str, str, str]]  # (yaml_path, old, new)

    @property
    def dirty(self) -> bool:
        return bool(self.spec_changes or self.contract_changes)


def _contract_range(first_party: dict[str, str]) -> str:
    key = _normalize_name(CONTRACT_PACKAGE)
    if key not in first_party:
        raise RuntimeError(f"{CONTRACT_PACKAGE} not found among workspace members")
    return derive_range(first_party[key])


def apply(root: Path) -> SyncReport:
    """Rewrite every member pyproject + every plugin descriptor in place. Idempotent."""
    members = discover_members(root)
    first_party = first_party_versions(members)
    contract_range = _contract_range(first_party)
    report = SyncReport(spec_changes=[], contract_changes=[])

    for member in members:
        py_path = member / "pyproject.toml"
        text = py_path.read_text()
        pyproject = tomllib.loads(text)
        changes = compute_pyproject_changes(pyproject, first_party)
        if changes:
            new_text, _ = rewrite_pyproject_text(text, changes)
            py_path.write_text(new_text)
            member_path = member.relative_to(root).as_posix()
            for change in changes:
                report.spec_changes.append((member_path, change))

    for yml in plugin_descriptor_files(members, root):
        text = yml.read_text()
        old = contract_yaml_value(text)
        new_text, changed = rewrite_contract_yaml(text, contract_range)
        if changed:
            yml.write_text(new_text)
            report.contract_changes.append(
                (yml.relative_to(root).as_posix(), old or "", contract_range)
            )

    _self_assert(root)
    return report


def _self_assert(root: Path) -> None:
    """After applying, re-derive and confirm every rewritten specifier and every
    contract pin now equals the formula output. Raises on any mismatch."""
    drift = check(root)
    if drift.dirty:
        details = "; ".join(
            f"{m}: {c.old_req} -> {c.new_req}" for m, c in drift.spec_changes
        ) or "; ".join(f"{p}: {o} -> {n}" for p, o, n in drift.contract_changes)
        raise RuntimeError(f"self-assert failed after apply: {details}")


def check(root: Path) -> SyncReport:
    """Verify every first-party specifier + contract pin already equals the
    formula output. Returns a report of any drift (does not modify files)."""
    members = discover_members(root)
    first_party = first_party_versions(members)
    contract_range = _contract_range(first_party)
    report = SyncReport(spec_changes=[], contract_changes=[])

    for member in members:
        pyproject = _load_toml(member / "pyproject.toml")
        member_path = member.relative_to(root).as_posix()
        for change in compute_pyproject_changes(pyproject, first_party):
            report.spec_changes.append((member_path, change))

    for yml in plugin_descriptor_files(members, root):
        text = yml.read_text()
        current = contract_yaml_value(text)
        if current is not None and current != contract_range:
            report.contract_changes.append(
                (yml.relative_to(root).as_posix(), current, contract_range)
            )

    return report


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #

def _repo_root() -> Path:
    """The repo root is the parent of this script's ``scripts/`` directory."""
    return Path(__file__).resolve().parent.parent


def _format_drift(report: SyncReport) -> str:
    lines: list[str] = []
    for member_path, change in report.spec_changes:
        lines.append(
            f"  {member_path}/pyproject.toml: "
            f"{change.old_req!r} -> {change.new_req!r}"
        )
    for yaml_path, old, new in report.contract_changes:
        lines.append(f"  {yaml_path}: contract {old!r} -> {new!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify ranges are already synced; exit 1 with a diff on drift",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="repo root (defaults to the parent of scripts/)",
    )
    args = parser.parse_args(argv)
    root = (args.root or _repo_root()).resolve()

    if args.check:
        report = check(root)
        if report.dirty:
            print("range-sync: OUT OF SYNC — first-party ranges do not match the formula:")
            print(_format_drift(report))
            print("\nRun `python scripts/range_sync.py` to fix.")
            return 1
        print("range-sync: all first-party ranges and contract pins are in sync.")
        return 0

    report = apply(root)
    if report.dirty:
        print("range-sync: rewrote first-party ranges:")
        print(_format_drift(report))
    else:
        print("range-sync: nothing to change; already in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
