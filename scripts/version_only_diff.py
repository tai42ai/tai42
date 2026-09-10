"""Classify a git diff as carrying nothing but release version bumps.

A release-please train — and the merge commit it lands on ``main`` — touches
only version strings: each released member's ``pyproject.toml`` ``version``
line, its ``tai-plugin.yml`` descriptor version, the release manifest, and the
first-party workspace ``version`` entries the lockfile records. Such a diff
adds no signal to the e2e backend matrix or the browser suite, so ci.yml gates
those heavy lanes off when this classifier reports a version-only diff. Any
other content — source, a dependency-range change, tests, a rebuilt frontend
bundle — makes the diff NOT version-only, so the heavy lanes run.

Exit 0 (version-only) or 1 (anything else); stdlib only, driven from the CI
``changes`` job over the ``base...head`` range.
"""

from __future__ import annotations

import argparse
import re
import subprocess

MANIFEST = ".release-please-manifest.json"
LOCKFILE = "uv.lock"

# Added/removed lines of a unified diff (``+++``/``---`` headers are dropped by
# the caller before these apply). A top-level ``version`` carries no diff-body
# indentation, so anchoring the marker directly against ``version`` rejects an
# indented (nested) version line.
_PYPROJECT_VERSION = re.compile(r'^[+-]version\s*=\s*"[^"]*"\s*$')
_DESCRIPTOR_VERSION = re.compile(r"^[+-]version:\s*\S.*$")
_MANIFEST_VERSION = re.compile(r'^[+-]\s*"[^"]+":\s*"[^"]*"\s*,?\s*$')
_LOCK_VERSION = re.compile(r'^[+-]version = "[^"]*"$')
_LOCK_NAME = re.compile(r'^[ +-]name = "([^"]*)"$')


def _changed_lines(diff: str) -> list[str]:
    """The added/removed content lines of a unified diff, minus the ``+++`` /
    ``---`` file headers."""
    return [line for line in diff.splitlines() if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]


def _lock_is_version_only(diff: str) -> bool:
    """A lockfile diff is version-only when every changed line is a ``version =``
    line and each sits in a ``[[package]]`` block whose ``name`` is first-party
    (``tai42-*``); a third-party pin or any non-version edit fails it."""
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


def _path_is_version_only(path: str, diff: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if path == MANIFEST:
        lines = _changed_lines(diff)
        return bool(lines) and all(_MANIFEST_VERSION.match(line) for line in lines)
    if path == LOCKFILE:
        return _lock_is_version_only(diff)
    if name == "pyproject.toml":
        lines = _changed_lines(diff)
        return bool(lines) and all(_PYPROJECT_VERSION.match(line) for line in lines)
    if name == "tai-plugin.yml":
        lines = _changed_lines(diff)
        return bool(lines) and all(_DESCRIPTOR_VERSION.match(line) for line in lines)
    return name == "CHANGELOG.md"


def is_version_only(changed: dict[str, str]) -> bool:
    """``changed`` maps each changed path to its unified ``git diff`` text; True
    iff at least one path changed and every changed path is a pure version
    bump."""
    if not changed:
        return False
    return all(_path_is_version_only(path, diff) for path, diff in changed.items())


def _git_diff(rng: str) -> dict[str, str]:
    names = subprocess.run(
        ["git", "diff", "--name-only", rng],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return {
        path: subprocess.run(
            ["git", "diff", rng, "--", path],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        for path in names
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exit 0 when a git range is a version-only diff, else 1.")
    parser.add_argument("--base", required=True, help="base commit of the range")
    parser.add_argument("--head", required=True, help="head commit of the range")
    args = parser.parse_args(argv)
    # Three-dot range so a moved base branch contributes no unrelated files: the
    # diff is what HEAD introduces over its merge base with BASE.
    changed = _git_diff(f"{args.base}...{args.head}")
    return 0 if is_version_only(changed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
