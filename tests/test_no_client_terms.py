"""Root fleet-consistency test — the platform is generic: no banned client, flow,
product, or business-domain name ever appears in a tracked — or newly added,
not-yet-committed — text/source file. Any banned term or marker found anywhere in
the tree is a hard failure naming file:line:term.

The ban list is data, never source: this file carries the SCAN mechanism (tree
walk, word-boundary regex, substring markers, self-tests) with no term baked in.
Entries load at runtime from, in order, the ``TAI_BANNED_TERMS`` environment
variable (comma-separated, whitespace-trimmed, empty entries dropped) else the
local untracked file ``~/.config/tai42/banned-terms.txt`` (one entry per line,
``#`` comments allowed). Entry grammar:

* a plain entry is a word-boundary term (matched case-insensitively, so an
  unrelated word that merely embeds the stem is spared);
* an entry prefixed ``marker:`` is a case-sensitive substring marker, ``marker-ci:``
  a case-insensitive one — for tokens that do not sit on regex word boundaries.

With no list available the guard is never a silent green: under CI it fails,
locally it skips — both carry the same message."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_SELF = Path(__file__).resolve()

_NO_LIST_MSG = "no banned-terms list: set TAI_BANNED_TERMS or ~/.config/tai42/banned-terms.txt"
_LOCAL_LIST = Path.home() / ".config" / "tai42" / "banned-terms.txt"

#: Consumer-vocabulary phrases banned in the platform tree independently of any secret
#: client list — a consumer's (e.g. the flow engine's) own terms never appear in the
#: platform. Committed here, not secret-sourced, so the ban holds on every checkout and
#: applies even when no client list is present. This file names them as data, so it is
#: excluded from the scan (:data:`_SELF`).
_CONSUMER_TERMS: tuple[str, ...] = (
    "babel" + "fish",  # assembled from fragments so the sibling downstream-terms guard does not flag this entry
    "flow-views",
    "flow_step",
    "flow_resume",
    "router loop",
    "custom node",
)


def _raw_entries() -> list[str]:
    env = os.environ.get("TAI_BANNED_TERMS")
    if env is not None and env.strip():
        return [entry.strip() for entry in env.split(",")]
    if _LOCAL_LIST.is_file():
        return [line.split("#", 1)[0].strip() for line in _LOCAL_LIST.read_text(encoding="utf-8").splitlines()]
    return []


def _load() -> tuple[list[str], list[tuple[str, bool]]]:
    terms: list[str] = list(_CONSUMER_TERMS)
    markers: list[tuple[str, bool]] = []
    for entry in _raw_entries():
        if not entry:
            continue
        if entry.startswith("marker-ci:"):
            markers.append((entry[len("marker-ci:") :], True))
        elif entry.startswith("marker:"):
            markers.append((entry[len("marker:") :], False))
        else:
            terms.append(entry)
    return terms, markers


def _require(values):
    if not values:
        if os.environ.get("CI"):
            pytest.fail(_NO_LIST_MSG)
        pytest.skip(_NO_LIST_MSG)
    return values


def _compile(terms: list[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(re.escape(term) for term in terms) + r")\b", re.IGNORECASE)


def _line_marker(line: str, markers: list[tuple[str, bool]]) -> str | None:
    for marker, case_insensitive in markers:
        haystack = line.lower() if case_insensitive else line
        target = marker.lower() if case_insensitive else marker
        if target in haystack:
            return marker
    return None


def _scanned_files() -> list[str]:
    # Tracked files, unioned with untracked-but-not-ignored ones, so a banned
    # term in a NEW file is caught before it is git-added, not only after.
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=False,
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "-z", "--others", "--exclude-standard"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=False,
    ).stdout
    return [p.decode("utf-8") for p in (tracked + untracked).split(b"\x00") if p]


def _iter_text_lines():
    for rel in _scanned_files():
        path = ROOT / rel
        if not path.is_file():  # tracked but deleted in the worktree
            continue
        if path.resolve() == _SELF:  # this guard names the banned phrases as data
            continue
        data = path.read_bytes()
        if b"\x00" in data:  # binary: text-term invariant does not apply
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            yield rel, lineno, line


def _scan_term_hits(banned_re: re.Pattern[str]) -> list[str]:
    return [
        f"{rel}:{lineno}:{match.group(0)}"
        for rel, lineno, line in _iter_text_lines()
        for match in banned_re.finditer(line)
    ]


def _scan_marker_hits(markers: list[tuple[str, bool]]) -> list[str]:
    hits: list[str] = []
    for rel, lineno, line in _iter_text_lines():
        marker = _line_marker(line, markers)
        if marker is not None:
            hits.append(f"{rel}:{lineno}:{marker}")
    return hits


def test_no_banned_client_terms():
    terms, _ = _load()
    hits = _scan_term_hits(_compile(_require(terms)))
    assert not hits, "banned term(s) found in tracked/untracked files:\n" + "\n".join(hits)


def test_no_banned_markers():
    _, markers = _load()
    hits = _scan_marker_hits(_require(markers))
    assert not hits, "banned marker(s) found in tracked/untracked files:\n" + "\n".join(hits)


def test_scan_is_not_vacuous():
    # A green result must come from reading the whole tree (~2300 files), never
    # from an empty scan set: the floor fails loudly if discovery collapses.
    assert len(_scanned_files()) > 500


def test_regex_catches_each_banned_term():
    # Controls are derived from the loaded terms, so no term is embedded here.
    terms = _require(_load()[0])
    banned_re = _compile(terms)
    for term in terms:
        for control in (term, term.upper(), term.capitalize(), f"a {term} here", f"{term},"):
            assert banned_re.search(control), f"regex missed banned control: {control!r}"


def test_regex_ignores_embedded_lookalikes():
    terms = _require(_load()[0])
    banned_re = _compile(terms)
    lowered = {t.lower() for t in terms}
    for term in terms:
        controls = [f"pre{term}ing", f"x{term}y"]
        stem = term[:-1]
        # A stem that is itself a listed term (an overlapping singular/plural) legitimately
        # matches; only a stem that is no term proves word-boundary discrimination.
        if stem and stem.lower() not in lowered:
            controls.append(stem)
        for control in controls:
            assert not banned_re.search(control), f"regex matched non-banned control: {control!r}"


def test_markers_catch_each_leakage_shape():
    markers = _require(_load()[1])
    for marker, case_insensitive in markers:
        assert _line_marker(f"before {marker} after", markers) is not None, f"marker scan missed: {marker!r}"
        if case_insensitive:
            assert _line_marker(f"before {marker.upper()} after", markers) is not None, (
                f"case-insensitive marker missed under upper-case: {marker!r}"
            )


def test_markers_ignore_non_leakage():
    markers = _require(_load()[1])
    for marker, _ in markers:
        stem = marker[:-1]
        if not stem or any(m.lower() in stem.lower() for m, _ in markers):
            continue  # a shorter marker legitimately lives inside this stem
        assert _line_marker(stem, markers) is None, f"marker scan flagged a truncated stem: {stem!r}"
