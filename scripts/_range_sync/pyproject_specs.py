"""Analysing a member pyproject into the first-party specifier rewrites to apply,
the pinned caps preserved across a major, and the unpinned cross-major warnings —
and applying the rewrites to the raw file text."""

from __future__ import annotations

from dataclasses import dataclass

from _range_sync.members import _normalize_name, _requirement_strings
from _range_sync.version_ranges import _pin_guarded, derive_range, parse_requirement

# The dependant-declared pin table key: ``[tool.range-sync] pinned = [...]``.
PIN_TABLE = "range-sync"
PIN_KEY = "pinned"


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
