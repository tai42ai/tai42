"""Classify a git diff as carrying nothing but release bookkeeping.

Release bookkeeping is what a release-please train writes beside the code it
releases: each released member's ``pyproject.toml`` ``version`` line, its
``tai-plugin.yml`` descriptor version and the ``version.txt`` a ``simple``
release type writes; the first-party ``tai42-*`` dependency ranges and
descriptor ``contract:`` pins ``scripts/range_sync.py`` derives from those
versions; the release manifest, the first-party workspace ``version``
entries the lockfile records, and the changelogs. Any other content — source, a
third-party dependency change, a first-party requirement added, removed or
re-marked, tests, a rebuilt frontend bundle — makes the diff NOT bookkeeping.

Two callers read it. ci.yml gates the heavy e2e backend matrix and browser suite
off a bookkeeping-only ``base...head`` range (the command line below: exit 0
for bookkeeping-only, 1 for anything else). The consumer boot gate reads a
released member's diff since its previous release tag: a member released for
bookkeeping alone publishes the code it already published, so its published
version is still booted against the candidate core.

Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

MANIFEST = ".release-please-manifest.json"
LOCKFILE = "uv.lock"

# The git subprocess bound: a hung git fails loudly instead of stalling the caller.
_GIT_TIMEOUT_S = 120

# Added/removed lines of a unified diff (``+++``/``---`` headers are dropped by
# the caller before these apply). A top-level key carries no diff-body
# indentation, so anchoring the marker directly against the key rejects an
# indented (nested) line.
_PYPROJECT_VERSION = re.compile(r'^[+-]version\s*=\s*"[^"]*"\s*$')
# One first-party requirement per line, as a dependency array writes it: the
# name with any extras, a version specifier, an optional environment marker.
_FIRST_PARTY_REQUIREMENT = re.compile(
    r'^[+-](?P<indent>\s*)"(?P<name>tai42-[A-Za-z0-9._-]+(?:\[[^\]]*\])?)\s*'
    r'[<>=!~][<>=!~0-9.,*\s]*(?P<marker>;[^"]*)?"(?P<comma>,?)\s*$'
)
_DESCRIPTOR_KEY = re.compile(r"^[+-](?P<key>version|contract):\s*\S.*$")
_VERSION_TXT = re.compile(r"^[+-]\d+\.\d+\.\d+\S*$")
_MANIFEST_VERSION = re.compile(r'^[+-]\s*"[^"]+":\s*"[^"]*"\s*,?\s*$')
_LOCK_VERSION = re.compile(r'^[+-]version = "[^"]*"$')
_LOCK_NAME = re.compile(r'^[ +-]name = "([^"]*)"$')


def _changed_lines(diff: str) -> list[str]:
    """The added/removed content lines of a unified diff, minus the ``+++`` / ``---`` file headers."""
    return [line for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]


def _rewrites_only(lines: list[str], normalize: Callable[[str], str | None]) -> bool:
    """True when every changed line is a bookkeeping value rewritten in place.

    Every line normalizes (``normalize`` returns ``None`` for a line outside the bookkeeping shapes), and
    the removed lines normalize to exactly the added ones: no value is added or dropped.
    """
    removed: Counter[str] = Counter()
    added: Counter[str] = Counter()
    for line in lines:
        key = normalize(line)
        if key is None:
            return False
        (removed if line.startswith("-") else added)[key] += 1
    return bool(lines) and removed == added


def _pyproject_key(line: str) -> str | None:
    if _PYPROJECT_VERSION.match(line):
        return "version"
    match = _FIRST_PARTY_REQUIREMENT.match(line)
    if match is None:
        return None
    return f'{match["indent"]}"{match["name"]}{match["marker"] or ""}"{match["comma"]}'


def _descriptor_key(line: str) -> str | None:
    match = _DESCRIPTOR_KEY.match(line)
    return match["key"] if match else None


def _lock_is_version_only(diff: str) -> bool:
    """A lockfile diff is bookkeeping when every changed line is a ``version =`` line in a first-party block.

    Each must sit in a ``[[package]]`` block whose ``name`` is first-party (``tai42-*``); a third-party pin
    or any non-version edit fails it. A first-party dependency is an editable workspace entry with no
    specifier, so a range rewrite never reaches the lock.
    """
    lines = diff.splitlines()
    saw_change = False
    for i, line in enumerate(lines):
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")):
            continue
        saw_change = True
        if not _LOCK_VERSION.match(line):
            return False
        name = None
        for prev in range(i, -1, -1):
            match = _LOCK_NAME.match(lines[prev])
            if match:
                name = match.group(1)
                break
        if name is None or not name.startswith("tai42-"):
            return False
    return saw_change


def _path_is_bookkeeping(path: str, diff: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if path == MANIFEST:
        lines = _changed_lines(diff)
        return bool(lines) and all(_MANIFEST_VERSION.match(line) for line in lines)
    if path == LOCKFILE:
        return _lock_is_version_only(diff)
    if name == "pyproject.toml":
        return _rewrites_only(_changed_lines(diff), _pyproject_key)
    if name == "tai-plugin.yml":
        return _rewrites_only(_changed_lines(diff), _descriptor_key)
    if name == "version.txt":
        lines = _changed_lines(diff)
        return bool(lines) and all(_VERSION_TXT.match(line) for line in lines)
    return name == "CHANGELOG.md"


def is_release_bookkeeping(changed: dict[str, str]) -> bool:
    """True iff at least one path changed and every changed path carries release bookkeeping alone.

    ``changed`` maps each changed path to its unified ``git diff`` text.
    """
    if not changed:
        return False
    return all(_path_is_bookkeeping(path, diff) for path, diff in changed.items())


def _git(args: list[str], cwd: Path | None) -> str:
    return subprocess.run(  # noqa: S603 fixed, trusted argv; no shell and no user input
        ["git", *args],  # noqa: S607 fixed, trusted executable resolved from PATH
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=_GIT_TIMEOUT_S,
    ).stdout


def git_diff(revisions: list[str], *, paths: tuple[str, ...] = (), cwd: Path | None = None) -> dict[str, str]:
    """Each path ``git diff <revisions> [-- <paths>]`` changes, mapped to its unified diff text."""
    scope = ["--", *paths] if paths else []
    names = _git(["diff", "--name-only", *revisions, *scope], cwd).split()
    return {path: _git(["diff", *revisions, "--", path], cwd) for path in names}


def main(argv: list[str] | None = None) -> int:
    """Exit 0 when the given git range carries release bookkeeping alone, else 1."""
    parser = argparse.ArgumentParser(description="Exit 0 when a git range carries release bookkeeping alone, else 1.")
    parser.add_argument("--base", required=True, help="base commit of the range")
    parser.add_argument("--head", required=True, help="head commit of the range")
    args = parser.parse_args(argv)
    # Three-dot range so a moved base branch contributes no unrelated files: the
    # diff is what HEAD introduces over its merge base with BASE.
    changed = git_diff([f"{args.base}...{args.head}"])
    return 0 if is_release_bookkeeping(changed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
