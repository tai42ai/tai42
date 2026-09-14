"""Governing-package version + bump-class verdicts, and the loud-fail helper
the whole gate raises through."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

from tai42_cli import api_gate


def _fail(message: str) -> NoReturn:
    print(f"::error::{message}", file=sys.stderr)
    raise SystemExit(1)


def read_project_version(member_dir: Path) -> str:
    """The ``project.version`` of a packaged member — the source of truth the
    release tag must match, read the same way the release workflow reads it."""
    import tomllib

    pyproject = member_dir / "pyproject.toml"
    if not pyproject.is_file():
        _fail(f"no pyproject.toml at {pyproject}")
    return tomllib.loads(pyproject.read_text())["project"]["version"]


def governing_bump(package: str, version: str, repo_root: Path) -> str:
    """The bump class of the governing package — its ``version`` against its
    previous released tag, via :mod:`tai42_cli.api_gate`'s tag/version plumbing. A package
    with no previous tag is a first release and returns ``"major"`` (an unbounded
    first release carries any surface). When the package is not bumped on a train,
    its version is the last released one, so the bump reads as that last release's
    class — never major — and a consumer break correctly fails the gate."""
    previous = api_gate._previous_tag(package, version, repo_root)
    if previous is None:
        return "major"
    previous_version = previous[len(f"{package}-v") :]
    return api_gate._bump_class(previous_version, version)


def break_is_accepted(bump: str) -> bool:
    """A consumer boot break may ship only in a major bump."""
    return bump == "major"
