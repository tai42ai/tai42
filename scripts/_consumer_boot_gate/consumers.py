"""Consumer distributions to boot.

The supplied wheels/reqs and the first-party plugin enumeration (each at its latest published PyPI version).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from tai42_cli import api_gate

from _consumer_boot_gate.candidate import read_release_layout, released_by_tree
from _consumer_boot_gate.versioning import _fail
from release_bookkeeping_diff import git_diff, is_release_bookkeeping

_WHEEL_NAME_RE = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*)-")


@dataclass(frozen=True)
class Consumer:
    """A consumer distribution to install and boot.

    Its distribution name (used to read the installed descriptor), a display label, and the install argument uv
    receives (a wheel path or a requirement spec).
    """

    dist_name: str
    label: str
    install_arg: str


def wheel_name_version(wheel: Path) -> tuple[str, str]:
    """The distribution name (hyphenated) and version parsed from a wheel filename."""
    match = _WHEEL_NAME_RE.match(wheel.name)
    if match is None:
        _fail(f"cannot parse a distribution name/version from wheel filename {wheel.name!r}")
    return match["name"].replace("_", "-"), match["version"]


def _req_dist_name(req: str) -> str:
    """The distribution name at the head of a requirement spec (``pkg==1.2`` -> ``pkg``), for the descriptor read."""
    return re.split(r"[<>=!~\[; ]", req, maxsplit=1)[0]


def collect_consumers(wheels: list[str], reqs: list[str]) -> list[Consumer]:
    """Resolve the supplied wheels and requirement specs into consumers.

    A wheel path that does not exist raises loudly — an upstream download that failed is a hard error, never a
    skipped consumer.
    """
    consumers: list[Consumer] = []
    for raw in wheels:
        wheel = Path(raw)
        if not wheel.is_file():
            _fail(f"consumer wheel not found at {wheel} — a failed download must not read as a pass")
        name, version = wheel_name_version(wheel)
        consumers.append(Consumer(dist_name=name, label=f"{name} {version}", install_arg=str(wheel.resolve())))
    consumers.extend(Consumer(dist_name=_req_dist_name(req), label=req, install_arg=req) for req in reqs)
    return consumers


_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def canonical_dist_name(name: str) -> str:
    """The normalized form of a distribution name, so two spellings of one distribution compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def member_dist_name(pyproject: Path) -> str:
    """The distribution name a member's ``pyproject.toml`` declares (``[project].name``)."""
    import tomllib

    return tomllib.loads(pyproject.read_text())["project"]["name"]


def first_party_plugin_names(repo_root: Path) -> dict[str, str]:
    """Every packaged first-party plugin, mapping its distribution name to its member dir.

    A descriptor-only plugin dir (no ``pyproject.toml``) ships no distribution and is skipped.
    """
    names: dict[str, str] = {}
    for pyproject in sorted(repo_root.glob("plugins/*/pyproject.toml")):
        names[member_dist_name(pyproject)] = str(pyproject.parent.relative_to(repo_root))
    return names


def _released_with_own_changes(package: str, path: str, version: str, repo_root: Path) -> bool:
    """Whether the tree releases ``package`` at ``version`` with changes of its own.

    True for a first release (nothing published to boot). Otherwise the member dir's diff from the
    package's previous release tag to HEAD decides: a diff of release bookkeeping alone (its version,
    the first-party ranges and contract pins those versions derive, its changelog) releases the code
    already published, so it is False. A released member whose dir is unchanged since that tag
    contradicts its own release and fails loudly.
    """
    previous = api_gate._previous_tag(package, version, repo_root)
    if previous is None:
        return True
    changed = git_diff([previous, "HEAD"], paths=(path,), cwd=repo_root)
    if not changed:
        _fail(f"{package} {version} is released by this tree, but {path} is unchanged since {previous}")
    return not is_release_bookkeeping(changed)


@dataclass(frozen=True)
class ReleasedPackages:
    """The distribution names this tree releases, split by what each release carries.

    ``own_changes`` changed beyond release bookkeeping since their previous release (or release for the
    first time); ``bookkeeping_only`` are released for their version and derived ranges alone.
    """

    own_changes: frozenset[str]
    bookkeeping_only: frozenset[str]


def released_packages(repo_root: Path) -> ReleasedPackages:
    """Every release-please package whose manifest version the tree releases (its tag absent or at HEAD).

    Booting an ``own_changes`` package's published version is pointless (its new code is the candidate,
    its new version unpublished), so the caller excludes it. A ``bookkeeping_only`` package publishes the
    code it already published, so it stays in the boot set at its published version.
    """
    packages, manifest = read_release_layout(repo_root)
    own: set[str] = set()
    bookkeeping: set[str] = set()
    for path, entry in packages.items():
        package = entry.get("package-name")
        version = manifest.get(path)
        if not package or not version or not released_by_tree(package, version, repo_root):
            continue
        (own if _released_with_own_changes(package, path, version, repo_root) else bookkeeping).add(package)
    return ReleasedPackages(own_changes=frozenset(own), bookkeeping_only=frozenset(bookkeeping))


def latest_pypi_version(name: str) -> str | None:
    """The highest final ``major.minor.patch`` release of ``name`` on PyPI, or ``None`` when it has none.

    ``None`` covers a distribution never published, or a 404.
    """
    import urllib.error
    import urllib.request

    url = f"https://pypi.org/pypi/{name}/json"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            releases = json.loads(resp.read()).get("releases", {})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    finals = [v for v, files in releases.items() if files and _VERSION_RE.match(v)]
    if not finals:
        return None
    return max(finals, key=lambda v: tuple(int(part) for part in v.split(".")))


def enumerate_first_party(repo_root: Path) -> tuple[list[Consumer], list[str]]:
    """The first-party plugin consumers to boot — each at its latest published PyPI version.

    Each is NOT released by this tree with changes of its own (see :func:`released_packages`). Plus the
    notices: one per plugin with no PyPI release yet (skipped, never a failure) and one per plugin this
    tree releases for its ranges alone (booted at its published version, whose own ranges decide whether
    it installs against the candidate core). One consumer per distribution.
    """
    released = released_packages(repo_root)
    consumers: list[Consumer] = []
    notices: list[str] = []
    for name in sorted(first_party_plugin_names(repo_root)):
        if name in released.own_changes:
            continue
        version = latest_pypi_version(name)
        if version is None:
            notices.append(f"{name}: no PyPI release yet — skipped (not a failure)")
            continue
        if name in released.bookkeeping_only:
            notices.append(
                f"{name}: released by this tree for its version and derived ranges alone — "
                f"booting its published {version}"
            )
        consumers.append(Consumer(dist_name=name, label=f"{name} {version}", install_arg=f"{name}=={version}"))
    return consumers, notices
