#!/usr/bin/env python3
"""Refuse a pull request whose release label understates the public-API change it carries.

release-please turns a merged pull request into version bumps, and the projection
here follows its squash parsing exactly. The squash message is the PR title (its
header) followed by the PR body, with two body constructs honored first: a
``BEGIN_COMMIT_OVERRIDE`` … ``END_COMMIT_OVERRIDE`` block replaces the ENTIRE message
(title included) with its own trimmed text, and ``BEGIN_NESTED_COMMIT`` …
``END_NESTED_COMMIT`` blocks are lifted out as their own messages (each read as a
single commit). release-please then splits the remaining message into chunks and
reads each chunk's own header. A new chunk begins ONLY at a blank line immediately
followed by a whitelisted ``type(scope)?: `` header — the ``!`` breaking marker is
not part of that split, so a ``type!:`` line in the body, or any line not preceded
by a blank line, stays plain body text and starts no chunk. Within a chunk the bump
is ``feat`` -> minor, ``fix`` -> patch, any other type -> no release, raised to a
major by a ``!`` on that chunk's own header or a ``BREAKING CHANGE:`` /
``BREAKING-CHANGE:`` footer inside the chunk. The projected bump is the highest
across all chunks. A ``Release-As: X.Y.Z`` footer overrides the computation and
forces every touched package to ``X.Y.Z``. The pre-major rules of
:data:`_BUMP_MINOR_PRE_MAJOR` / :data:`_BUMP_PATCH_FOR_MINOR_PRE_MAJOR`, read from
the release-please config, apply while a package is below ``1.0.0``. A file under a
package's path attributes the pull request to that package.

For each first-party package the pull request touches, this check computes the
version release-please would publish and runs the release API-diff gate
(``tai42-api-gate``) against that projected version — the same gate ``release.yml``
runs on the tag, moved before the merge. A breaking public-API change under a bump
the projected label could not honestly carry fails the check with the gate's own
message, so the mislabel is caught before the tag rather than after it. A touched
package whose directory has no ``pyproject.toml`` is descriptor-only (its release
artifact is a descriptor, not a wheel) and has no Python surface to diff, so it is
reported and skipped — the same split ``release.yml`` makes. When no first-party
package is touched, or none of the touched packages projects a release, the check
passes.

The package list, their directories and the pre-major flags come from
``release-please-config.json``; the current versions from
``.release-please-manifest.json`` — org values live in the repo's own config, never
here. The pull request title and body are read from the ``PR_TITLE`` / ``PR_BODY``
environment variables (never interpolated into a shell), and the changed files
from ``git diff --name-only <base>...HEAD``.
"""

from __future__ import annotations

import argparse
import enum
import json
import os
import re
import subprocess
from pathlib import Path

# release-please starts a new bumping chunk only at a blank line immediately
# followed by a whitelisted ``type(scope)?: `` header. The ``!`` breaking marker is
# NOT in this lookahead, so a ``type!:`` line never begins a chunk — it stays body
# text of the chunk it sits in. The whitelist is release-please's own set of types.
_CHUNK_SPLIT = re.compile(
    r"\r?\n\r?\n(?=(?:feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(?:\(.*?\))?: )"
)
# A conventional header: ``type(scope)!: summary``. The scope and the ``!`` marker are
# optional; only the type and a ``!`` on the header itself bear on the bump.
_HEADER = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s")
# A ``BREAKING CHANGE:`` / ``BREAKING-CHANGE:`` footer inside a chunk forces it to a major.
_BREAKING_FOOTER = re.compile(r"^BREAKING[ -]CHANGE:", re.MULTILINE)
# A ``Release-As: <version>`` footer forces every touched package to that exact version.
_RELEASE_AS = re.compile(r"^Release-As:\s*v?(?P<version>\S+)", re.IGNORECASE | re.MULTILINE)
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

# release-please config keys for the pre-1.0 bump rules; both default off, which
# makes a below-1.0 package follow plain semver (breaking -> major, feat -> minor).
_BUMP_MINOR_PRE_MAJOR = "bump-minor-pre-major"
_BUMP_PATCH_FOR_MINOR_PRE_MAJOR = "bump-patch-for-minor-pre-major"


class Bump(enum.IntEnum):
    """A release bump level, ordered so the highest across a message wins."""

    NONE = 0
    PATCH = 1
    MINOR = 2
    MAJOR = 3


def _header_bump(line: str) -> Bump:
    """Bump a single conventional header line projects, or :attr:`Bump.NONE` when it is not one.

    ``!`` marks a breaking change (major); ``feat`` is a minor and ``fix`` a patch;
    every other type releases nothing on its own.
    """
    match = _HEADER.match(line)
    if match is None:
        return Bump.NONE
    if match.group("bang"):
        return Bump.MAJOR
    kind = match.group("type").lower()
    if kind == "feat":
        return Bump.MINOR
    if kind == "fix":
        return Bump.PATCH
    return Bump.NONE


def _squash_message(title: str, body: str) -> str:
    """The squash message release-please reads: the title, then the body a blank line below."""
    return f"{title}\n\n{body}" if body else title


def _apply_override(body: str) -> str | None:
    """The text of a ``BEGIN_COMMIT_OVERRIDE`` block in the body, or ``None`` when there is none.

    release-please lets a pull request body override the whole commit message: the text
    between ``BEGIN_COMMIT_OVERRIDE`` and ``END_COMMIT_OVERRIDE``, trimmed, replaces the
    entire message (title included) when it is non-empty.
    """
    if "BEGIN_COMMIT_OVERRIDE" not in body:
        return None
    override = body.split("BEGIN_COMMIT_OVERRIDE")[1].split("END_COMMIT_OVERRIDE")[0].strip()
    return override or None


def _split_messages(message: str) -> list[str]:
    """Split a message into its chunks the way release-please does.

    ``BEGIN_NESTED_COMMIT`` / ``END_NESTED_COMMIT`` blocks are lifted out as their own
    messages (each parsed as a single commit, not re-split); the text outside them is
    chunk-split at every blank line followed by a whitelisted ``type(scope)?: `` header.
    """
    parts = message.split("BEGIN_NESTED_COMMIT")
    base = parts[0]
    nested: list[str] = []
    for part in parts[1:]:
        segments = part.split("END_NESTED_COMMIT")
        nested.append(segments[0])
        base += "END_NESTED_COMMIT".join(segments[1:])
    chunks = [chunk for chunk in _CHUNK_SPLIT.split(base) if chunk]
    return [*chunks, *nested]


def _chunk_bump(chunk: str) -> Bump:
    """The bump one message chunk projects, from its own header and any footer it carries.

    The header is the chunk's first non-empty line; a ``!`` there is a major. Otherwise a
    ``BREAKING CHANGE:`` / ``BREAKING-CHANGE:`` footer inside the chunk makes it a major;
    failing both, the header type alone decides (``feat`` minor, ``fix`` patch).
    """
    header = next((line for line in chunk.splitlines() if line.strip()), "")
    bump = _header_bump(header)
    if bump is Bump.MAJOR:
        return bump
    if _BREAKING_FOOTER.search(chunk):
        return Bump.MAJOR
    return bump


def projected_bump(title: str, body: str) -> Bump:
    """The bump release-please projects from a pull request's title and body.

    A ``BEGIN_COMMIT_OVERRIDE`` block in the body replaces the whole message first; the
    resulting message is then split into chunks release-please's way — nested-commit
    blocks lifted out, the rest chunk-split at each blank line followed by a whitelisted
    ``type(scope)?: `` header — and the result is the highest bump across every chunk
    (see :func:`_apply_override`, :func:`_split_messages`, :func:`_chunk_bump`).
    """
    override = _apply_override(body)
    message = override if override is not None else _squash_message(title, body)
    bump = Bump.NONE
    for chunk in _split_messages(message):
        bump = max(bump, _chunk_bump(chunk))
    return bump


def release_as(title: str, body: str) -> str | None:
    """The version a ``Release-As:`` footer forces, or ``None`` when the pull request has none.

    Read from the effective message — a ``BEGIN_COMMIT_OVERRIDE`` block replaces it first,
    so a ``Release-As:`` inside the override wins and one outside a present override is gone.
    """
    override = _apply_override(body)
    message = override if override is not None else _squash_message(title, body)
    match = _RELEASE_AS.search(message)
    return match.group("version") if match else None


def load_packages(config_path: Path) -> dict[str, dict[str, object]]:
    """Map each package directory to its release-please config entry."""
    config = json.loads(config_path.read_text())
    packages = config.get("packages")
    if not isinstance(packages, dict) or not packages:
        raise SystemExit(f"::error::{config_path} declares no packages")
    global_defaults = {
        _BUMP_MINOR_PRE_MAJOR: bool(config.get(_BUMP_MINOR_PRE_MAJOR, False)),
        _BUMP_PATCH_FOR_MINOR_PRE_MAJOR: bool(config.get(_BUMP_PATCH_FOR_MINOR_PRE_MAJOR, False)),
    }
    resolved: dict[str, dict[str, object]] = {}
    for directory, entry in packages.items():
        merged: dict[str, object] = dict(global_defaults)
        merged.update(entry)
        resolved[directory] = merged
    return resolved


def touched_dirs(changed_files: list[str], package_dirs: list[str]) -> set[str]:
    """Package directories a change touches, each file attributed to its longest matching dir."""
    touched: set[str] = set()
    for changed in changed_files:
        best: str | None = None
        for directory in package_dirs:
            if (changed == directory or changed.startswith(f"{directory}/")) and (
                best is None or len(directory) > len(best)
            ):
                best = directory
        if best is not None:
            touched.add(best)
    return touched


def project_version(
    current: str, bump: Bump, *, bump_minor_pre_major: bool, bump_patch_for_minor_pre_major: bool
) -> str | None:
    """The version release-please would publish from ``current`` for ``bump``, or ``None`` for no release.

    Below ``1.0.0`` the two pre-major flags redirect a breaking change to a minor and a
    feature to a patch respectively; at or above ``1.0.0`` plain semver applies.
    """
    if bump is Bump.NONE:
        return None
    match = _VERSION.match(current)
    if match is None:
        raise SystemExit(f"::error::manifest version {current!r} is not a bare major.minor.patch")
    major, minor, patch = (int(part) for part in match.groups())
    pre_major = major == 0
    if bump is Bump.MAJOR:
        if pre_major and bump_minor_pre_major:
            return f"{major}.{minor + 1}.0"
        return f"{major + 1}.0.0"
    if bump is Bump.MINOR:
        if pre_major and bump_patch_for_minor_pre_major:
            return f"{major}.{minor}.{patch + 1}"
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def changed_files(base_ref: str, repo_root: Path) -> list[str]:
    """Files the pull request changes, from ``git diff --name-only <base>...HEAD``."""
    out = subprocess.run(  # noqa: S603 fixed, trusted argv; no shell and no user input
        ["git", "diff", "--name-only", f"{base_ref}...HEAD"],  # noqa: S607 trusted executable resolved from PATH
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.splitlines() if line.strip()]


def _run_gate(package: str, directory: str, version: str, repo_root: Path) -> tuple[int, str]:
    """Run ``tai42-api-gate`` for one projected release; return its exit code and combined output."""
    result = subprocess.run(  # noqa: S603 fixed, trusted argv; no shell and no user input
        [  # noqa: S607 trusted console script resolved from PATH
            "tai42-api-gate",
            "--package",
            package,
            "--dir",
            directory,
            "--version",
            version,
            "--repo-root",
            str(repo_root),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def _projected_version(directory: str, entry: dict[str, object], manifest: dict[str, str], bump: Bump) -> str | None:
    """Projected version for one touched package, reading its current version from the manifest."""
    try:
        current = manifest[directory]
    except KeyError:
        raise SystemExit(f"::error::{directory} has no entry in the release-please manifest") from None
    return project_version(
        current,
        bump,
        bump_minor_pre_major=bool(entry[_BUMP_MINOR_PRE_MAJOR]),
        bump_patch_for_minor_pre_major=bool(entry[_BUMP_PATCH_FOR_MINOR_PRE_MAJOR]),
    )


def check(
    title: str,
    body: str,
    files: list[str],
    packages: dict[str, dict[str, object]],
    manifest: dict[str, str],
    repo_root: Path,
) -> int:
    """Gate every touched package's projected release; return a non-zero code when any is dishonest.

    Prints each package's projection and the gate's own output; a gate refusal is
    echoed with the remediation (raise the release label to the bump the gate names).
    """
    bump = projected_bump(title, body)
    forced = release_as(title, body)
    touched = touched_dirs(files, list(packages))
    if not touched:
        print("release-label-check: no first-party package touched — nothing to gate.")
        return 0
    releasing: dict[str, str] = {}
    for directory in sorted(touched):
        # A Release-As footer forces the version for every touched package,
        # overriding the projected bump (release-please releases it regardless).
        version = forced if forced is not None else _projected_version(directory, packages[directory], manifest, bump)
        if version is not None:
            releasing[directory] = version
    if not releasing:
        print(
            f"release-label-check: a {bump.name.lower()} bump projects no release for the "
            "touched package(s) — nothing to gate."
        )
        return 0
    label = f"forced to {forced} by Release-As" if forced is not None else f"{bump.name.lower()} bump"
    failed = False
    for directory, version in releasing.items():
        package = str(packages[directory]["package-name"])
        # A dir without a pyproject.toml is descriptor-only: its release artifact is a
        # descriptor, not a wheel, so it has no Python surface to diff. release.yml skips
        # the gate for it, and so must this check — the gate raises on a missing src/.
        if not (repo_root / directory / "pyproject.toml").is_file():
            print(
                f"release-label-check: {package} ({directory}) is descriptor-only "
                "(no pyproject.toml, no Python API surface) — skipped."
            )
            continue
        print(f"release-label-check: {package} ({directory}) projects {version} ({label})")
        code, output = _run_gate(package, directory, version, repo_root)
        print(output, end="" if output.endswith("\n") else "\n")
        if code != 0:
            failed = True
            print(
                f"::error::{package}: the pull request's release ({label}) cannot honestly carry this change. "
                "Raise the release to the bump the gate names above — a breaking public-API change needs a major "
                "release (add '!' to the commit type or a 'BREAKING CHANGE:' footer)."
            )
    return 1 if failed else 0


def main() -> int:
    """Parse arguments, load config + manifest, and gate the pull request's projected releases."""
    parser = argparse.ArgumentParser(
        description="Refuse a pull request whose release label understates its API change."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="root of the tree to gate")
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="release-please config (default: <repo-root>/release-please-config.json)",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="release-please manifest (default: <repo-root>/.release-please-manifest.json)",
    )
    parser.add_argument("--base-ref", default="origin/main", help="base ref the pull request diffs against")
    args = parser.parse_args()

    repo_root = args.repo_root.resolve()
    config_path = args.config or repo_root / "release-please-config.json"
    manifest_path = args.manifest or repo_root / ".release-please-manifest.json"

    title = os.environ.get("PR_TITLE")
    if not title:
        raise SystemExit("::error::PR_TITLE is not set; cannot read the pull request's release label")
    body = os.environ.get("PR_BODY", "")

    packages = load_packages(config_path)
    manifest = json.loads(manifest_path.read_text())
    files = changed_files(args.base_ref, repo_root)
    return check(title, body, files, packages, manifest, repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
