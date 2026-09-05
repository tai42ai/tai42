"""Root fleet-consistency test — the platform is generic: no client, flow, or
product name ever appears in a tracked — or newly added, not-yet-committed —
text/source file. Any banned client term found anywhere in the tree is a hard
failure naming file:line:term.

The banned terms are assembled from fragments so this guard file satisfies the
very invariant it enforces (its own source must not contain the substrings)."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_BANNED_CLIENT_TERMS = (
    "conci" + "erge",
    "bookin" + "guru",
    "bookin" + "-guru",
    "bookin" + "_guru",
    "invo" + "ice",
)

_BANNED_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(term) for term in _BANNED_CLIENT_TERMS) + r")\b",
    re.IGNORECASE,
)


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


def _scan_hits() -> list[str]:
    hits: list[str] = []
    for rel in _scanned_files():
        path = ROOT / rel
        if not path.is_file():  # tracked but deleted in the worktree
            continue
        data = path.read_bytes()
        if b"\x00" in data:  # binary: text-term invariant does not apply
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in _BANNED_RE.finditer(line):
                hits.append(f"{rel}:{lineno}:{match.group(0)}")
    return hits


def test_no_banned_client_terms():
    hits = _scan_hits()
    assert not hits, "banned client term(s) found in tracked/untracked files:\n" + "\n".join(hits)


def test_scan_is_not_vacuous():
    # A green result must come from reading the whole tree (~2300 files), never
    # from an empty scan set: the floor fails loudly if discovery collapses.
    assert len(_scanned_files()) > 500


# Control strings assembled from the same fragment style as the ban list, so
# this guard file stays self-exempt and greps clean.
_CONCI = "conci" + "erge"
_GURU = "bookin" + "guru"
_INVO = "invo" + "ice"

_MATCHING_CONTROLS = (
    _CONCI,  # exact, lowercase
    _CONCI.upper(),  # all caps
    _CONCI.capitalize(),  # title case
    "Bookin" + "Guru",  # mixed-case product name
    "bookin" + "-guru",  # hyphenated
    "bookin" + "_guru",  # underscored
    "a " + _CONCI + " here",  # bounded by spaces
    _CONCI + ",",  # trailing punctuation is a word boundary
    _INVO,  # business-domain term, lowercase
    _INVO.capitalize(),  # title case
)

_NON_MATCHING_CONTROLS = (
    "pre" + _CONCI + "ing",  # embedded: no word boundary
    "x" + _GURU + "y",  # embedded product name
    _CONCI[:-1],  # truncated stem, not the whole term
)


def test_regex_catches_each_banned_term():
    for control in _MATCHING_CONTROLS:
        assert _BANNED_RE.search(control), f"regex missed banned control: {control!r}"


def test_regex_ignores_embedded_lookalikes():
    for control in _NON_MATCHING_CONTROLS:
        assert not _BANNED_RE.search(control), f"regex matched non-banned control: {control!r}"
