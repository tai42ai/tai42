"""Consumer distributions to boot: the supplied wheels/reqs and the first-party
plugin enumeration (each at its latest published PyPI version)."""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from _consumer_boot_gate.versioning import _fail

_WHEEL_NAME_RE = re.compile(r"^(?P<name>.+?)-(?P<version>\d[^-]*)-")


@dataclass(frozen=True)
class Consumer:
    """A consumer distribution to install and boot: its distribution name (used to
    read the installed descriptor), a display label, and the install argument uv
    receives (a wheel path or a requirement spec)."""

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
    """The distribution name at the head of a requirement spec (``pkg==1.2`` ->
    ``pkg``), used to read the installed descriptor."""
    return re.split(r"[<>=!~\[; ]", req, maxsplit=1)[0]


def collect_consumers(wheels: list[str], reqs: list[str]) -> list[Consumer]:
    """Resolve the supplied wheels and requirement specs into consumers. A wheel
    path that does not exist raises loudly — an upstream download that failed is a
    hard error, never a skipped consumer."""
    consumers: list[Consumer] = []
    for raw in wheels:
        wheel = Path(raw)
        if not wheel.is_file():
            _fail(f"consumer wheel not found at {wheel} — a failed download must not read as a pass")
        name, version = wheel_name_version(wheel)
        consumers.append(Consumer(dist_name=name, label=f"{name} {version}", install_arg=str(wheel.resolve())))
    for req in reqs:
        consumers.append(Consumer(dist_name=_req_dist_name(req), label=req, install_arg=req))
    return consumers


_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def first_party_plugin_names(repo_root: Path) -> dict[str, str]:
    """Every packaged first-party plugin, mapping its distribution name to its member
    dir. A descriptor-only plugin dir (no ``pyproject.toml``) ships no distribution and
    is skipped."""
    import tomllib

    names: dict[str, str] = {}
    for pyproject in sorted(repo_root.glob("plugins/*/pyproject.toml")):
        name = tomllib.loads(pyproject.read_text())["project"]["name"]
        names[name] = str(pyproject.parent.relative_to(repo_root))
    return names


def release_bump_set(repo_root: Path) -> set[str]:
    """The distribution names being RELEASED in this train — a package whose
    release-please manifest version has no matching tag yet (a pending bump). Read the
    way the API gate reads versions: the manifest version against the package's tags.
    Booting such a package's stale published version is pointless (its new code is the
    candidate, its new version unpublished), so the caller excludes it."""
    import json

    manifest = json.loads((repo_root / ".release-please-manifest.json").read_text())
    config = json.loads((repo_root / "release-please-config.json").read_text())
    bumped: set[str] = set()
    for path, entry in config["packages"].items():
        package = entry.get("package-name")
        version = manifest.get(path)
        if not package or not version:
            continue
        tags = subprocess.run(
            ["git", "tag", "--list", f"{package}-v{version}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if not tags:
            bumped.add(package)
    return bumped


def latest_pypi_version(name: str) -> str | None:
    """The highest final ``major.minor.patch`` release of ``name`` on PyPI, or ``None``
    when the distribution has no release yet (never published, or a 404)."""
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
    """The first-party plugin consumers to boot — each NOT in this train's bump set, at
    its latest published PyPI version — plus a notice per plugin with no PyPI release yet
    (skipped, never a failure). One consumer per distribution."""
    bumped = release_bump_set(repo_root)
    consumers: list[Consumer] = []
    notices: list[str] = []
    for name in sorted(first_party_plugin_names(repo_root)):
        if name in bumped:
            continue
        version = latest_pypi_version(name)
        if version is None:
            notices.append(f"{name}: no PyPI release yet — skipped (not a failure)")
            continue
        consumers.append(Consumer(dist_name=name, label=f"{name} {version}", install_arg=f"{name}=={version}"))
    return consumers, notices
