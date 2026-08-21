#!/usr/bin/env python3
"""Derive first-party ``tai42-*`` version ranges from the released member
versions and rewrite them in place, so ranges never go stale by hand.

Two surfaces are kept in lockstep with the released versions:

  (A) first-party ``tai42-*`` dependency ranges in every workspace member's
      ``pyproject.toml`` (``[project].dependencies`` +
      ``[project.optional-dependencies]``);
  (B) the ``contract:`` pin in every ``tai-plugin.yml`` (both the root copy and
      the packaged ``src/.../tai-plugin.yml`` copy of each plugin, plus the root
      copy of every DESCRIPTOR-ONLY component — a workspace-glob dir carrying a
      ``tai-plugin.yml`` but no ``pyproject.toml``, so it ships no package and is
      never a member), which tracks ``tai42-contract``. A descriptor-only
      component carries no pyproject pin to preserve, so its descriptor follows
      the GLOBAL derived contract range exactly like an unpinned member's.

The version source of truth is each member's ``[project].version``. The range
for a released version ``V`` is derived by a single rule (patch is ignored):

  * floor = ``<major>.<minor>`` always (minor precision; the released minor)
  * cap   = ``0.<minor+1>`` pre-1.0 (major == 0), else ``<major+1>`` from 1.0

e.g. 0.3.0 -> ``>=0.3,<0.4``; 0.5.1 -> ``>=0.5,<0.6``; 1.2.3 -> ``>=1.2,<2``.

Files are edited by targeted string/line replacement (not a toml/yaml
round-trip) so formatting and comments are preserved. Reading uses ``tomllib``.
Pure Python standard library only; no clock, network, or randomness.

A dependant may mark a first-party cap as deliberate with a
``[tool.range-sync] pinned = ["<dep-name>", ...]`` table in its own
``pyproject.toml``. A pinned dep whose derived range would cross a MAJOR at
either end — the floor major or the cap major, so a deliberately WIDENED cap
(``>=1.2,<3``) counts as much as a raised floor, and a ``~=``/``==`` spec whose
implied majors move counts too — is left untouched (and reported) instead of
rewritten; a pinned dep whose existing spec has an unparseable major structure
is likewise preserved conservatively rather than rewritten blind. Minor/patch
syncs within one major ignore the pin and behave identically for every dep. The
pin decision is read from the same ``tomllib`` parse that discovers
requirements, so it is robust regardless of the text-level rewrite path (a line
comment would be invisible to that parse). An unpinned dep whose rewrite is
guarded (crosses at either end, or unparseable) still syncs, but ``--check``
emits a non-failing WARNING so a deliberate cap can be annotated. When a pin
preserves a member's ``tai42-contract`` range, that member's ``contract:``
descriptor follows the preserved dep (derived from its floor) rather than the
global released range; when the preserved spec has no derivable floor the
descriptor is left untouched entirely (and reported) rather than forced to the
global range — so a pinned member never advertises a contract major its
dependency refuses. A ``pinned`` name that is not a first-party dependency of
that member is a loud error, and a ``[tool.range-sync]`` table on a non-member
pyproject (e.g. the root) is a loud error too — it would silently do nothing.

CLI:
  python scripts/range_sync.py            # APPLY: rewrite in place (default)
  python scripts/range_sync.py --check    # verify sync; exit 1 + diff on drift
"""

from __future__ import annotations

import argparse
import re
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
_CONTRACT_RE = re.compile(r"^(?P<indent>\s*)contract:\s*(?P<q>['\"])(?P<val>.*?)(?P=q)(?P<trail>\s*)$")

# The integer major of a ``>=<major>...`` floor and a ``<<major>...`` cap. The
# derived range always carries both; a hand-written spec may carry either, both,
# or (for ``~=``/``==``/anything else) neither in this shape.
_FLOOR_RE = re.compile(r">=\s*(\d+)")
_CAP_RE = re.compile(r"<\s*(\d+)")

# A ``~=X.Y`` or ``==X.Y.Z`` spec: both its floor and its cap major are X.
_COMPAT_RE = re.compile(r"[~=]=\s*(\d+)")

# The floor VERSION literal (major[.minor[.patch]]) of a ``>=``/``~=``/``==``
# spec — used to re-derive a descriptor range from a preserved dependency spec.
_FLOOR_VERSION_RE = re.compile(r"(?:>=|~=|==)\s*(\d+(?:\.\d+)*)")

# The dependant-declared pin table key: ``[tool.range-sync] pinned = [...]``.
PIN_TABLE = "range-sync"
PIN_KEY = "pinned"


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


def _major_structure(spec: str) -> tuple[int | None, int | None]:
    """The ``(floor_major, cap_major)`` integer majors of a specifier; each is
    None when that end is absent or unparseable. A ``~=X.Y`` / ``==X.Y.Z`` spec
    implies floor and cap majors both X; otherwise the floor comes from ``>=``
    and the cap from ``<``."""
    s = spec.strip()
    if not s:
        return (None, None)
    compat = _COMPAT_RE.search(s)
    if compat:
        major = int(compat.group(1))
        return (major, major)
    floor = _FLOOR_RE.search(s)
    cap = _CAP_RE.search(s)
    return (
        int(floor.group(1)) if floor else None,
        int(cap.group(1)) if cap else None,
    )


def is_cross_major(old_spec: str, new_spec: str) -> bool:
    """True when *old_spec* and *new_spec* differ in EITHER the floor's integer
    major OR the cap's integer major — a deliberately widened cap (``<3`` derived
    down to ``<2``) crosses as surely as a raised floor. Pre-1.0 both majors are
    0, so a 0.x minor bump never crosses. A major that is absent at one end (no
    comparable value) does not, by itself, make that end differ."""
    old_floor, old_cap = _major_structure(old_spec)
    new_floor, new_cap = _major_structure(new_spec)
    floor_differs = old_floor is not None and new_floor is not None and old_floor != new_floor
    cap_differs = old_cap is not None and new_cap is not None and old_cap != new_cap
    return floor_differs or cap_differs


def _pin_guarded(old_spec: str, derived: str) -> bool:
    """A rewrite a pin should preserve (and an unpinned cap should warn on): the
    majors cross (floor or cap), or the existing spec's major structure is
    unparseable — the latter treated conservatively as a crossing rather than
    rewritten blind. An empty spec is version-less and never guarded."""
    if is_cross_major(old_spec, derived):
        return True
    return bool(old_spec.strip()) and _major_structure(old_spec) == (None, None)


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


@dataclass(frozen=True)
class Preserved:
    """A pinned first-party cap left untouched across a major bump."""

    dep_name: str
    kept_range: str


@dataclass
class PyprojectAnalysis:
    """The outcome of analysing one pyproject: the rewrites to apply, the
    pinned caps preserved across a major, and the unpinned caps that crossed a
    major (a subset of ``changes``, surfaced as ``--check`` warnings)."""

    changes: list[SpecChange]
    preserved: list[Preserved]
    warnings: list[SpecChange]


def pinned_deps(pyproject: dict) -> set[str]:
    """The normalized first-party names marked deliberate in this member's
    ``[tool.range-sync] pinned`` list. Raises loudly if the table value is not a
    list of strings."""
    table = pyproject.get("tool", {}).get(PIN_TABLE, {})
    raw = table.get(PIN_KEY, [])
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise RuntimeError(f"[tool.{PIN_TABLE}].{PIN_KEY} must be a list of dependency-name strings")
    return {_normalize_name(x) for x in raw}


def _first_party_dep_names(pyproject: dict, first_party: dict[str, str]) -> set[str]:
    """Normalized names of this member's first-party dependencies (any table
    scanned as a source), regardless of whether they carry a specifier."""
    present: set[str] = set()
    for raw in _requirement_strings(pyproject):
        parsed = parse_requirement(raw)
        if parsed is None:
            continue
        key = _normalize_name(parsed.name)
        if key in first_party:
            present.add(key)
    return present


def analyze_pyproject(
    pyproject: dict, first_party: dict[str, str], member_label: str = "<pyproject>"
) -> PyprojectAnalysis:
    """Analyse one parsed pyproject. Version-less and non-first-party refs are
    skipped; a rewrite is computed only when old != derived. A guarded rewrite
    (floor or cap major crosses, or the existing spec's major structure is
    unparseable) is preserved for a pinned dep (kept out of ``changes``); an
    unpinned guarded rewrite still applies but is also flagged as a warning.

    Raises if a ``pinned`` name is not a first-party dependency of this member —
    a malformed annotation is never silently ignored."""
    pinned = pinned_deps(pyproject)
    unknown = pinned - _first_party_dep_names(pyproject, first_party)
    if unknown:
        raise RuntimeError(
            f"{member_label}: [tool.{PIN_TABLE}].{PIN_KEY} names are not first-party "
            f"dependencies of this member: {sorted(unknown)}"
        )

    changes: dict[str, SpecChange] = {}
    preserved: dict[str, Preserved] = {}
    warnings: dict[str, SpecChange] = {}
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
        if new_req == raw:
            continue
        change = SpecChange(parsed.name, raw, new_req)
        guarded = _pin_guarded(parsed.specifier, derived)
        if guarded and key in pinned:
            preserved[raw] = Preserved(parsed.name, parsed.specifier)
            continue  # deliberate cap: leave untouched
        changes[raw] = change
        if guarded:
            warnings[raw] = change
    return PyprojectAnalysis(
        changes=list(changes.values()),
        preserved=list(preserved.values()),
        warnings=list(warnings.values()),
    )


def compute_pyproject_changes(pyproject: dict, first_party: dict[str, str]) -> list[SpecChange]:
    """Return the set of first-party specifier rewrites for one parsed
    pyproject (the rewrites to apply, with pinned cross-major caps excluded)."""
    return analyze_pyproject(pyproject, first_party).changes


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
            raise RuntimeError(f"could not locate requirement literal {change.old_req!r} to rewrite")
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


def plugin_descriptor_files(members: list[Path], root: Path) -> list[tuple[Path, Path]]:
    """Every ``(member, tai-plugin.yml)`` pair (root + packaged copies) under each
    plugin member — the owning member is carried so a member-specific contract
    range (a preserved pin) can override the global one."""
    files: list[tuple[Path, Path]] = []
    for member in members:
        if member.relative_to(root).parts[0] != "plugins":
            continue
        for yml in sorted(member.rglob("tai-plugin.yml")):
            files.append((member, yml))
    return files


def descriptor_only_contract_files(root: Path) -> list[Path]:
    """Every descriptor-only component's root ``tai-plugin.yml``: a workspace-glob
    dir that carries a ``tai-plugin.yml`` but NO ``pyproject.toml`` (the connector
    dirs the root pyproject lists under ``[tool.uv.workspace].exclude`` — they
    ship no package, so ``discover_members`` never sees them). Discovery mirrors
    the packaged split: same globs, partitioned on the presence of a pyproject.
    Each such component carries no pyproject pin to preserve, so its descriptor
    follows the GLOBAL derived contract range exactly like an unpinned member."""
    files: list[Path] = []
    for pattern in workspace_globs(root):
        for hit in sorted(root.glob(pattern)):
            if hit.is_dir() and (hit / "tai-plugin.yml").is_file() and not (hit / "pyproject.toml").is_file():
                files.append(hit / "tai-plugin.yml")
    return sorted(set(files))


# --------------------------------------------------------------------------- #
# Apply / check                                                               #
# --------------------------------------------------------------------------- #


@dataclass
class SyncReport:
    """What an apply run changed / what a check run found out of sync.

    ``preserved``, ``warnings`` and ``descriptor_untouched`` are informational
    only: a preserved pin is not a drift, a warning does not fail the gate, and
    a descriptor left untouched under an underivable-floor pin is not a rewrite —
    so none of them feed ``dirty``."""

    spec_changes: list[tuple[str, SpecChange]]  # (member_path, change)
    contract_changes: list[tuple[str, str, str]]  # (yaml_path, old, new)
    preserved: list[tuple[str, Preserved]]  # (member_path, preserved)
    warnings: list[tuple[str, SpecChange]]  # (member_path, unpinned cross-major change)
    descriptor_untouched: list[tuple[str, str]]  # (yaml_path, left-at contract value)

    @property
    def dirty(self) -> bool:
        return bool(self.spec_changes or self.contract_changes)


def _contract_range(first_party: dict[str, str]) -> str:
    key = _normalize_name(CONTRACT_PACKAGE)
    if key not in first_party:
        raise RuntimeError(f"{CONTRACT_PACKAGE} not found among workspace members")
    return derive_range(first_party[key])


def _descriptor_range_from_spec(spec: str) -> str | None:
    """The contract range a descriptor should advertise given a preserved
    ``tai42-contract`` dependency *spec*: derived from the spec's floor version
    exactly as the global range derives from a released version. None when the
    spec has no parseable floor — the descriptor is then left untouched rather
    than forced to a guessed range."""
    m = _FLOOR_VERSION_RE.search(spec)
    if not m:
        return None
    return derive_range(m.group(1))


def _preserved_contract_ranges(members: list[Path], first_party: dict[str, str], root: Path) -> dict[str, str | None]:
    """Map member-path -> the contract range that member's descriptors must
    advertise, for every member whose ``tai42-contract`` dependency a pin
    preserved. The value is the range derived from the preserved spec's floor;
    it is None when that spec has no derivable floor — an explicit leave-alone
    marker so apply/check skip rewriting that member's descriptor entirely
    rather than forcing it to the global range the pin refuses. (Absence from
    the map, by contrast, means the member is unpinned and follows the global
    range.)"""
    contract_key = _normalize_name(CONTRACT_PACKAGE)
    out: dict[str, str | None] = {}
    for member in members:
        pyproject = _load_toml(member / "pyproject.toml")
        member_path = member.relative_to(root).as_posix()
        analysis = analyze_pyproject(pyproject, first_party, member_path)
        for preserved in analysis.preserved:
            if _normalize_name(preserved.dep_name) != contract_key:
                continue
            out[member_path] = _descriptor_range_from_spec(preserved.kept_range)
    return out


def _assert_no_stray_pin_tables(root: Path, members: list[Path]) -> None:
    """A ``[tool.range-sync]`` table is only honoured on a workspace MEMBER
    pyproject. The same table on any non-member pyproject the script reads (the
    root) would silently do nothing — raise so it is never mistaken for an
    active pin."""
    member_dirs = {m.resolve() for m in members}
    stray: list[str] = []
    root_py = root / "pyproject.toml"
    if root_py.is_file() and root.resolve() not in member_dirs and PIN_TABLE in _load_toml(root_py).get("tool", {}):
        stray.append(root_py.relative_to(root).as_posix())
    if stray:
        raise RuntimeError(f"pin tables belong on member pyprojects: {stray}")


def apply(root: Path) -> SyncReport:
    """Rewrite every member pyproject + every plugin descriptor in place. Idempotent."""
    members = discover_members(root)
    _assert_no_stray_pin_tables(root, members)
    first_party = first_party_versions(members)
    contract_range = _contract_range(first_party)
    preserved_contract = _preserved_contract_ranges(members, first_party, root)
    report = SyncReport(spec_changes=[], contract_changes=[], preserved=[], warnings=[], descriptor_untouched=[])

    for member in members:
        py_path = member / "pyproject.toml"
        text = py_path.read_text()
        pyproject = tomllib.loads(text)
        member_path = member.relative_to(root).as_posix()
        analysis = analyze_pyproject(pyproject, first_party, member_path)
        for preserved in analysis.preserved:
            report.preserved.append((member_path, preserved))
        for warning in analysis.warnings:
            report.warnings.append((member_path, warning))
        if analysis.changes:
            new_text, _ = rewrite_pyproject_text(text, analysis.changes)
            py_path.write_text(new_text)
            for change in analysis.changes:
                report.spec_changes.append((member_path, change))

    for member, yml in plugin_descriptor_files(members, root):
        member_path = member.relative_to(root).as_posix()
        yaml_path = yml.relative_to(root).as_posix()
        text = yml.read_text()
        old = contract_yaml_value(text)
        if member_path in preserved_contract:
            target = preserved_contract[member_path]
            if target is None:
                report.descriptor_untouched.append((yaml_path, old or ""))
                continue  # underivable-floor pin: descriptor left untouched entirely
        else:
            target = contract_range
        new_text, changed = rewrite_contract_yaml(text, target)
        if changed:
            yml.write_text(new_text)
            report.contract_changes.append((yaml_path, old or "", target))

    for yml in descriptor_only_contract_files(root):
        yaml_path = yml.relative_to(root).as_posix()
        text = yml.read_text()
        old = contract_yaml_value(text)
        new_text, changed = rewrite_contract_yaml(text, contract_range)
        if changed:
            yml.write_text(new_text)
            report.contract_changes.append((yaml_path, old or "", contract_range))

    _self_assert(root)
    return report


def _self_assert(root: Path) -> None:
    """After applying, re-derive and confirm every rewritten specifier and every
    contract pin now equals the formula output. Raises on any mismatch."""
    drift = check(root)
    if drift.dirty:
        details = "; ".join(f"{m}: {c.old_req} -> {c.new_req}" for m, c in drift.spec_changes) or "; ".join(
            f"{p}: {o} -> {n}" for p, o, n in drift.contract_changes
        )
        raise RuntimeError(f"self-assert failed after apply: {details}")


def check(root: Path) -> SyncReport:
    """Verify every first-party specifier + contract pin already equals the
    formula output. Returns a report of any drift (does not modify files)."""
    members = discover_members(root)
    _assert_no_stray_pin_tables(root, members)
    first_party = first_party_versions(members)
    contract_range = _contract_range(first_party)
    preserved_contract = _preserved_contract_ranges(members, first_party, root)
    report = SyncReport(spec_changes=[], contract_changes=[], preserved=[], warnings=[], descriptor_untouched=[])

    for member in members:
        pyproject = _load_toml(member / "pyproject.toml")
        member_path = member.relative_to(root).as_posix()
        analysis = analyze_pyproject(pyproject, first_party, member_path)
        for change in analysis.changes:
            report.spec_changes.append((member_path, change))
        for preserved in analysis.preserved:
            report.preserved.append((member_path, preserved))
        for warning in analysis.warnings:
            report.warnings.append((member_path, warning))

    for member, yml in plugin_descriptor_files(members, root):
        member_path = member.relative_to(root).as_posix()
        yaml_path = yml.relative_to(root).as_posix()
        text = yml.read_text()
        current = contract_yaml_value(text)
        if member_path in preserved_contract:
            target = preserved_contract[member_path]
            if target is None:
                report.descriptor_untouched.append((yaml_path, current or ""))
                continue  # underivable-floor pin: descriptor not flagged, left untouched
        else:
            target = contract_range
        if current is not None and current != target:
            report.contract_changes.append((yaml_path, current, target))

    for yml in descriptor_only_contract_files(root):
        yaml_path = yml.relative_to(root).as_posix()
        current = contract_yaml_value(yml.read_text())
        if current is not None and current != contract_range:
            report.contract_changes.append((yaml_path, current, contract_range))

    return report


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def _repo_root() -> Path:
    """The repo root is the parent of this script's ``scripts/`` directory."""
    return Path(__file__).resolve().parent.parent


def _print_preserved(report: SyncReport) -> None:
    for member_path, preserved in report.preserved:
        print(f"range-sync: {member_path}/pyproject.toml: {preserved.dep_name} pinned, left at {preserved.kept_range}")


def _print_descriptor_untouched(report: SyncReport) -> None:
    for yaml_path, kept in report.descriptor_untouched:
        print(f"range-sync: {yaml_path}: descriptor left untouched (pinned, underivable floor), at {kept!r}")


def _format_drift(report: SyncReport) -> str:
    lines: list[str] = []
    for member_path, change in report.spec_changes:
        lines.append(f"  {member_path}/pyproject.toml: {change.old_req!r} -> {change.new_req!r}")
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
        _print_preserved(report)
        _print_descriptor_untouched(report)
        for member_path, change in report.warnings:
            print(
                f"range-sync: WARNING: {member_path}/pyproject.toml: cross-major rewrite of an "
                f"unannotated cap for {change.dep_name}: {change.old_req!r} -> {change.new_req!r} — "
                f"annotate the cap in [tool.{PIN_TABLE}].{PIN_KEY} if it is deliberate."
            )
        if report.dirty:
            print("range-sync: OUT OF SYNC — first-party ranges do not match the formula:")
            print(_format_drift(report))
            print("\nRun `python scripts/range_sync.py` to fix.")
            return 1
        print("range-sync: all first-party ranges and contract pins are in sync.")
        return 0

    report = apply(root)
    _print_preserved(report)
    _print_descriptor_untouched(report)
    if report.dirty:
        print("range-sync: rewrote first-party ranges:")
        print(_format_drift(report))
    else:
        print("range-sync: nothing to change; already in sync.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
