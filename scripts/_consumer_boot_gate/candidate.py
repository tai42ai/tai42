"""The candidate's core release set: which core packages the tree releases, its bump, its previous release.

The candidate is the core the gate installs editable from the tree. Each core member dir maps to its
release-please ``package-name`` (``release-please-config.json``) and its version
(``.release-please-manifest.json``). A version is released by this tree when its tag
``<package>-v<version>`` is absent (a pending release) or points at HEAD (the commit a release's tags
are minted on); a version tagged on another commit is already published and contributes no bump. A
released package's bump is read the way :mod:`tai42_cli.api_gate` reads it — its version against its
previous released tag, a first release reading major — and the candidate's bump is the highest class
among the core packages the tree releases, ``none`` when it releases none.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from tai42_cli import api_gate

from _consumer_boot_gate.process import GIT_TIMEOUT_S, run_gate_step
from _consumer_boot_gate.versioning import _fail

_BUMP_ORDER = ("none", "patch", "minor", "major")


@dataclass(frozen=True)
class CoreRelease:
    """One core package as the candidate tree carries it.

    ``bump`` is its release class when the tree releases it, ``None`` when its version is already
    published. ``baseline_tag`` is the tag of its last published version before this tree: its current
    tag when the tree does not release it, its previous tag when it does (``None`` for a first release).
    """

    package: str
    version: str
    bump: str | None
    baseline_tag: str | None

    def describe(self) -> str:
        return f"{self.package} {self.version} ({self.bump or 'published'})"


@dataclass(frozen=True)
class Candidate:
    """The candidate core's release set."""

    members: tuple[CoreRelease, ...]

    @property
    def bump(self) -> str:
        """The highest bump class among the core packages the tree releases; ``none`` when it releases none."""
        bumps = [member.bump for member in self.members if member.bump is not None]
        return max(bumps, key=_BUMP_ORDER.index, default="none")

    def describe(self) -> str:
        members = ", ".join(member.describe() for member in self.members)
        verdict = "no core package released" if self.bump == "none" else f"{self.bump} bump"
        return f"candidate core {members} — {verdict}"


def _git_stdout(argv: list[str], *, what: str, repo_root: Path) -> str:
    result = run_gate_step(argv, what=what, timeout=GIT_TIMEOUT_S, cwd=repo_root, capture_output=True, text=True)
    if result.returncode != 0:
        _fail(f"{what} failed: {result.stderr.strip()[-400:]}")
    return result.stdout.strip()


def released_by_tree(package: str, version: str, repo_root: Path) -> bool:
    """True when the tree releases ``package`` at ``version``: its tag is absent or points at HEAD."""
    tag = f"{package}-v{version}"
    if not _git_stdout(["git", "tag", "--list", tag], what=f"listing the tag {tag}", repo_root=repo_root):
        return True
    tagged = _git_stdout(
        ["git", "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
        what=f"reading the commit of the tag {tag}",
        repo_root=repo_root,
    )
    head = _git_stdout(["git", "rev-parse", "--verify", "HEAD"], what="reading HEAD", repo_root=repo_root)
    return tagged == head


def read_release_layout(repo_root: Path) -> tuple[dict, dict]:
    """The release-please config's ``packages`` map and the manifest's dir -> version map."""
    config = json.loads((repo_root / "release-please-config.json").read_text())
    manifest = json.loads((repo_root / ".release-please-manifest.json").read_text())
    return config["packages"], manifest


def read_candidate(repo_root: Path, core_dirs: list[str]) -> Candidate:
    """Read the candidate's core release set from the tree.

    A core dir with no release-config ``package-name`` or no manifest version fails loudly: the gate
    cannot read the release of a core package it cannot name.
    """
    packages, manifest = read_release_layout(repo_root)
    members: list[CoreRelease] = []
    for raw_dir in core_dirs:
        core_dir = Path(raw_dir).as_posix()
        package = packages.get(core_dir, {}).get("package-name")
        if not package:
            _fail(f"core dir {core_dir} has no package-name in release-please-config.json")
        version = manifest.get(core_dir)
        if not version:
            _fail(f"core dir {core_dir} has no version in .release-please-manifest.json")
        if released_by_tree(package, version, repo_root):
            previous = api_gate._previous_tag(package, version, repo_root)
            bump = "major" if previous is None else api_gate._bump_class(previous[len(f"{package}-v") :], version)
            members.append(CoreRelease(package, version, bump, previous))
        else:
            members.append(CoreRelease(package, version, None, f"{package}-v{version}"))
    return Candidate(members=tuple(members))


def _is_ancestor(ancestor: str, descendant: str, repo_root: Path) -> bool:
    result = run_gate_step(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        what=f"checking whether {ancestor} is an ancestor of {descendant}",
        timeout=GIT_TIMEOUT_S,
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        _fail(f"git merge-base --is-ancestor {ancestor} {descendant} failed: {result.stderr.strip()[-400:]}")
    return result.returncode == 0


def previous_release_tag(candidate: Candidate, repo_root: Path) -> str | None:
    """The tag of the previous released platform tree: the core baseline tag every other one is an ancestor of.

    At that commit every core package sits at its last published version. ``None`` when no core package
    has a published version. Baseline tags no single one of which descends from all the others name no
    previous tree, which fails loudly.
    """
    tags = sorted({member.baseline_tag for member in candidate.members if member.baseline_tag is not None})
    for tag in tags:
        if all(other == tag or _is_ancestor(other, tag, repo_root) for other in tags):
            return tag
    if tags:
        _fail(f"no core baseline tag descends from all the others ({', '.join(tags)}) — no previous release tree")
    return None
