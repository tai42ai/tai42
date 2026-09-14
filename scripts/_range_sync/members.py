"""Workspace discovery: the member globs, the member dirs, and the released
first-party version map every range derives from."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# The workspace member globs. Kept in sync with the root pyproject
# ``[tool.uv.workspace].members``; read from disk at runtime (below) so this
# constant is only the fallback default.
DEFAULT_WORKSPACE_GLOBS = ["core/*", "plugins/*", "e2e"]

# The first-party distribution prefix. Only requirements whose base name starts
# with this AND resolve to a known member version are rewritten.
FIRST_PARTY_PREFIX = "tai42-"


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
