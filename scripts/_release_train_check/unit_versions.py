"""The version a release unit declares, read from the working tree or from a git revision.

A workspace member declares it as ``[project].version`` in its ``pyproject.toml``; a
descriptor-only component (no ``pyproject.toml``) as the top-level ``version:`` of its
``tai-plugin.yml``. A unit that declares no version is an error, never a silent default.
"""

from __future__ import annotations

import re
import subprocess
import tomllib
from pathlib import Path

# The git subprocess bound: a hung git fails loudly instead of stalling the check.
_GIT_TIMEOUT_S = 120

# A top-level ``version:`` key of a descriptor, its value optionally quoted, an optional comment.
_DESCRIPTOR_VERSION = re.compile(r"""^version:\s*(?P<q>['"]?)(?P<val>[^'"\s#]+)(?P=q)\s*(?:#.*)?$""")


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(  # noqa: S603 fixed, trusted argv; no shell and no user input
        ["git", *args],  # noqa: S607 fixed, trusted executable resolved from PATH
        check=True,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=_GIT_TIMEOUT_S,
    ).stdout


def version_file(root: Path, unit: str) -> str:
    """The repo-relative file *unit*'s version lives in, by the unit's kind in the working tree."""
    name = "pyproject.toml" if (root / unit / "pyproject.toml").is_file() else "tai-plugin.yml"
    return f"{unit}/{name}"


def _parse(file: str, text: str, where: str) -> str:
    if file == "pyproject.toml":
        version = tomllib.loads(text).get("project", {}).get("version")
        if not isinstance(version, str) or not version:
            raise ValueError(f"release-train: {where} declares no [project].version")
        return version
    for line in text.splitlines():
        match = _DESCRIPTOR_VERSION.match(line)
        if match:
            return match["val"]
    raise ValueError(f"release-train: {where} declares no top-level version")


def verify_revision(root: Path, revision: str) -> None:
    """Raise unless *revision* names a commit in the repository at *root*."""
    try:
        _git(["rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}"], root)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"release-train: {revision!r} is not a commit in {root}") from exc


def working_tree_version(root: Path, unit: str) -> str:
    """The version *unit* declares in the working tree at *root*."""
    rel = version_file(root, unit)
    return _parse(rel.rsplit("/", 1)[-1], (root / rel).read_text(), rel)


def revision_version(root: Path, revision: str, unit: str) -> str | None:
    """The version *unit* declares at *revision*, or ``None`` when the unit's version file is absent there.

    The file kind is the unit's kind in the working tree, so a unit that changed kind reads as absent.
    """
    rel = version_file(root, unit)
    if not _git(["ls-tree", "--name-only", revision, "--", rel], root).strip():
        return None
    return _parse(rel.rsplit("/", 1)[-1], _git(["show", f"{revision}:{rel}"], root), f"{revision}:{rel}")
