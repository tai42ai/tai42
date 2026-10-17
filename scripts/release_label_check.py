#!/usr/bin/env python3
"""Refuse a pull request whose release would publish a public-API change its version cannot carry.

The versions to check come from one of two sources:

* A release-train pull request — the one release-please opens, from the head
  branch release-please names for this config (``scripts/release-please/train-branches.js``)
  in this repository — is checked at the versions its ``.release-please-manifest.json``
  sets: every entry that differs from the base, or is new, is the version
  release-please tags when the train merges.
* Any other pull request is projected by release-please itself
  (``scripts/release-please/projection.js``): the squash commit GitHub will write
  for it, on top of ``main`` at the base sha, so every unreleased commit already on
  ``main`` counts as it will in the train (each with the pull request GitHub
  associates with it, read with ``GITHUB_TOKEN``). Each package the pull request
  touches that release-please would release is checked at that version; a package
  with a pending release the pull request does not touch is reported and not checked.

Each version is checked by the release API-diff gate (``tai42-api-gate``), the gate
``release.yml`` runs on the tag. A package whose directory has no ``pyproject.toml``
is descriptor-only (no Python surface to diff) and is skipped, as ``release.yml``
skips it.

Inputs: the squash headline and body GitHub renders for the pull request, with the
repository's allowed merge methods, from a GraphQL response file (``--squash-json``);
the pull request's own body from ``PR_BODY`` (never interpolated into a shell); its
head branch and head repository; the changed files from
``git diff --name-only <base>...HEAD``. The package list comes from
``release-please-config.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict, cast

MANIFEST = ".release-please-manifest.json"
CONFIG = "release-please-config.json"
TRAIN_LABEL = "the release train's manifest"


class ProjectedPath(TypedDict):
    """One package in the projection's result."""

    component: str
    version: str | None
    touched: bool


class Projection(TypedDict):
    """The projection's result: the release-please version that ran it, and each package it reports."""

    releasePlease: str
    paths: dict[str, ProjectedPath]


@dataclass(frozen=True)
class PullRequest:
    """The pull request as release-please will read it once squash-merged."""

    number: int
    headline: str
    body: str
    pr_body: str
    head_ref: str
    head_repository: str
    base_branch: str


def is_release_train(pr: PullRequest, repository: str, train_branches: list[str]) -> bool:
    """Whether ``pr`` comes from a release-please release branch of ``repository``."""
    return pr.head_repository == repository and pr.head_ref in train_branches


def load_packages(config_path: Path) -> dict[str, str]:
    """Map each package directory in the release-please config to its package name."""
    packages: Any = json.loads(config_path.read_text()).get("packages")
    if not isinstance(packages, dict) or not packages:
        raise SystemExit(f"::error::{config_path} declares no packages")
    entries = cast("dict[str, dict[str, Any]]", packages)
    return {directory: str(entry["package-name"]) for directory, entry in entries.items()}


def read_squash(path: Path) -> tuple[str, str]:
    """The squash headline and body from the GraphQL response at ``path``.

    Refuses a missing or incomplete response, and a repository that allows a merge
    method other than squash: the projection models a squash merge only.
    """
    try:
        repository = json.loads(path.read_text())["data"]["repository"]
        pull_request = repository["pullRequest"]
        headline = pull_request["viewerMergeHeadlineText"]
        body = pull_request["viewerMergeBodyText"]
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"::error::cannot read the squash message from {path}: {error!r}") from None
    if not isinstance(headline, str) or not headline or not isinstance(body, str):
        raise SystemExit(f"::error::{path} does not carry the squash headline and body as text")
    if repository.get("mergeCommitAllowed") or repository.get("rebaseMergeAllowed"):
        raise SystemExit(
            "::error::this check projects a squash merge; the repository also allows merge commits / "
            "rebase merges, whose commits release-please reads one by one"
        )
    return headline, body


def _git(repo_root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 fixed, trusted argv; no shell and no user input
        ["git", *args],  # noqa: S607 trusted executable resolved from PATH
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def changed_files(base_ref: str, repo_root: Path) -> list[str]:
    """Files the pull request changes, from ``git diff --name-only <base>...HEAD``."""
    return [line for line in _git(repo_root, "diff", "--name-only", f"{base_ref}...HEAD").splitlines() if line.strip()]


def _manifest_at(repo_root: Path, ref: str) -> dict[str, str]:
    return json.loads(_git(repo_root, "show", f"{ref}:{MANIFEST}"))


def _run_node(script: Path, request: dict[str, object]) -> Any:
    """Run one of the release-please scripts with ``request`` on stdin; return its JSON result."""
    result = subprocess.run(  # noqa: S603 fixed, trusted argv; the request travels on stdin
        ["node", str(script)],  # noqa: S607 trusted executable resolved from PATH
        input=json.dumps(request),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(f"::error::{script.name} failed:\n{result.stderr}")
    # release-please's warnings, shown in the job log.
    sys.stderr.write(result.stderr)
    return json.loads(result.stdout)


def versions_to_gate(
    repo_root: Path, base: str, tree: str, pr: PullRequest, *, repository: str, release_please: Path
) -> dict[str, tuple[str, str]]:
    """Map each package directory to check to its version and where that version comes from.

    ``release_please`` is the directory of the release-please scripts.
    """
    changed = changed_files(base, repo_root)
    branches = _run_node(
        release_please / "train-branches.js", {"repo": str(repo_root), "base": base, "baseBranch": pr.base_branch}
    )["branches"]
    if is_release_train(pr, repository, branches):
        before = _manifest_at(repo_root, base)
        after = _manifest_at(repo_root, tree)
        return {
            directory: (version, TRAIN_LABEL)
            for directory, version in after.items()
            if before.get(directory) != version
        }
    request: dict[str, object] = {
        "repo": str(repo_root),
        "repository": repository,
        "tree": tree,
        "base": base,
        "pullRequest": {
            "number": pr.number,
            "headline": pr.headline,
            "body": pr.body,
            "prBody": pr.pr_body,
            "files": changed,
        },
    }
    projection: Projection = _run_node(release_please / "projection.js", request)
    label = f"projected by release-please {projection['releasePlease']}"
    gated: dict[str, tuple[str, str]] = {}
    for directory, entry in projection["paths"].items():
        version = entry["version"]
        if version is None:
            continue
        if entry["touched"]:
            gated[directory] = (version, label)
        else:
            print(
                f"release-label-check: {directory} projects {version}, pending in the train, "
                "not touched by this pull request — not gated here."
            )
    return gated


def _run_gate(package: str, directory: str, version: str, repo_root: Path) -> tuple[int, str]:
    """Run ``tai42-api-gate`` for one release; return its exit code and combined output."""
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


def check(gated: dict[str, tuple[str, str]], packages: dict[str, str], repo_root: Path) -> int:
    """Run the API-diff gate for every version in ``gated``; return non-zero when any refuses.

    Prints each version checked and the gate's own output; a refusal is echoed with
    the remediation.
    """
    if not gated:
        print("release-label-check: this pull request releases no first-party package — nothing to gate.")
        return 0
    failed = False
    for directory, (version, label) in sorted(gated.items()):
        if directory not in packages:
            raise SystemExit(f"::error::{directory} is not a package in {CONFIG}")
        package = packages[directory]
        # A dir without a pyproject.toml is descriptor-only: its release artifact is a
        # descriptor, not a wheel, so it has no Python surface to diff. release.yml skips
        # the gate for it, and so must this check — the gate raises on a missing src/.
        if not (repo_root / directory / "pyproject.toml").is_file():
            print(
                f"release-label-check: {package} ({directory}) is descriptor-only "
                "(no pyproject.toml, no Python API surface) — skipped."
            )
            continue
        print(f"release-label-check: {package} ({directory}) releases {version} ({label})")
        code, output = _run_gate(package, directory, version, repo_root)
        print(output, end="" if output.endswith("\n") else "\n")
        if code != 0:
            failed = True
            print(
                f"::error::{package}: release {version} ({label}) cannot carry this change. "
                "Raise the squash message's bump to the one the gate names above — a '!' on the commit "
                "type or a 'BREAKING CHANGE:' footer in the commit message (the PR body is not part of "
                "the squash message)."
            )
    return 1 if failed else 0


def main() -> int:
    """Parse arguments, work out the versions this pull request releases, and gate each one."""
    parser = argparse.ArgumentParser(
        description="Refuse a pull request whose release cannot carry its public-API change."
    )
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="root of the tree to gate")
    parser.add_argument("--base-ref", required=True, help="the pull request's base sha")
    parser.add_argument("--pr-number", type=int, required=True, help="the pull request's number")
    parser.add_argument("--repository", required=True, help="the repository as owner/name")
    parser.add_argument("--base-branch", required=True, help="the pull request's base branch")
    parser.add_argument("--head-ref", required=True, help="the pull request's head branch")
    parser.add_argument(
        "--head-repository", default="", help="the head branch's repository as owner/name (empty when deleted)"
    )
    parser.add_argument("--squash-json", type=Path, help="GraphQL response with the squash headline and body")
    parser.add_argument(
        "--release-please-dir",
        type=Path,
        default=None,
        help="the release-please scripts (default: <repo-root>/scripts/release-please)",
    )
    args = parser.parse_args()

    if args.squash_json is None:
        raise SystemExit("::error::--squash-json is required: the squash message GitHub will write")
    headline, body = read_squash(args.squash_json)
    repo_root = args.repo_root.resolve()
    release_please = args.release_please_dir or repo_root / "scripts" / "release-please"
    packages = load_packages(repo_root / CONFIG)
    pr = PullRequest(
        number=args.pr_number,
        headline=headline,
        body=body,
        pr_body=os.environ.get("PR_BODY", ""),
        head_ref=args.head_ref,
        head_repository=args.head_repository,
        base_branch=args.base_branch,
    )
    gated = versions_to_gate(
        repo_root, args.base_ref, "HEAD", pr, repository=args.repository, release_please=release_please
    )
    return check(gated, packages, repo_root)


if __name__ == "__main__":
    raise SystemExit(main())
