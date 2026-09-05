"""Root fleet-consistency test — every tracked ``CHANGELOG.md`` anywhere in the
tree is the Keep-a-Changelog header with one empty ``[Unreleased]`` section, and
nothing else. Any file that diverges by even a byte is a hard failure naming its
path."""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CANONICAL_STUB = (
    "# Changelog\n"
    "\n"
    "All notable changes to this project will be documented in this file.\n"
    "\n"
    "The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),\n"
    "and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).\n"
    "\n"
    "## [Unreleased]\n"
)


def _changelog_files() -> list[str]:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=False,
    ).stdout
    return [p.decode("utf-8") for p in tracked.split(b"\x00") if p and p.decode("utf-8").endswith("CHANGELOG.md")]


def test_every_changelog_is_the_canonical_stub():
    expected = CANONICAL_STUB.encode("utf-8")
    offenders: list[str] = []
    for rel in _changelog_files():
        path = ROOT / rel
        if not path.is_file():  # tracked but deleted in the worktree
            continue
        if path.read_bytes() != expected:
            offenders.append(rel)
    assert not offenders, "CHANGELOG.md files that are not the canonical stub:\n" + "\n".join(offenders)


def test_scan_is_not_vacuous():
    # A green result must come from actually finding the fleet's CHANGELOG files,
    # never from an empty scan set: the floor fails loudly if discovery collapses.
    assert len(_changelog_files()) > 20
