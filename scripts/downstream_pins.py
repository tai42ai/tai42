#!/usr/bin/env python3
"""Check the fleet's downstream repos for a ``tai42-<core>`` pin that EXCLUDES a
just-released CORE member version, and open a tracking issue on this repo when
one does — so a downstream never goes red (or latent-stale) unnoticed after a
core major/minor, the way it used to until a hand-audit caught it.

The audited downstreams and the manifests to read are supplied by CONFIG
(``DOWNSTREAM_PINS_MANIFESTS`` JSON, or a ``DOWNSTREAM_PINS_FILE`` path to the
same) so this source names no specific private downstream; the CI workflow
injects the map from a repository variable. For each
manifest we fetch it over the GitHub contents API, parse every requirement out
of ``[project].dependencies`` / ``[project.optional-dependencies]`` /
``[dependency-groups]`` / ``[tool.uv].dev-dependencies`` with ``packaging`` (so
extras like ``tai42-kit[llm]>=3.5,<4`` and the version-less path/source lines are
handled correctly), keep only the CORE members, and test the released version
against each range with ``packaging.specifiers``.

Targets (which ``pkg==version`` pairs to test):

  * a component-tag push (``tai42-<core>-v<ver>``): the single released pair
    parsed straight out of the tag name;
  * a manual ``workflow_dispatch``: every CORE member at its current released
    version, read from ``release-please-config.json`` + each member's
    ``pyproject.toml`` on the checked-out tree.

On a violation we open — or, deduped by exact title, update — an issue titled
``downstream pin excludes <pkg> <ver>`` listing every offending repo/file/line/
range. We NEVER edit a downstream repo: widening a downstream pin is that
downstream maintainer's call. Pure Python standard library + ``packaging``; the
only side effects are HTTPS reads and the issue write.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

API = "https://api.github.com"
OWNER = "tai42ai"
REPO = "tai42"

# The CORE fleet members whose release can strand a downstream excluding pin.
# A tag outside this set (e.g. a plugin) is ignored by the trigger and here.
CORE = ("tai42-contract", "tai42-kit", "tai42-skeleton", "tai42-cli", "tai42-agents")

# Set to "1" to print the issues that WOULD be opened instead of writing them
# (used to validate the checker without touching the tracker).
DRY_RUN = os.environ.get("DOWNSTREAM_PINS_DRY_RUN") == "1"


def downstream_manifests() -> dict[str, list[str]]:
    """The downstream ``repo -> [manifest paths]`` map to audit, read from CONFIG so this
    source names no specific private downstream. ``DOWNSTREAM_PINS_MANIFESTS`` carries the JSON
    map inline; ``DOWNSTREAM_PINS_FILE`` points at a JSON file holding it (the inline var
    wins). Explicit, not discovered: the map is a reviewed configuration value, never scanned
    from a repo we were not told to read. Absent both, the map is empty and the sweep is a
    harmless no-op, so an unconfigured checkout never crashes."""
    raw = os.environ.get("DOWNSTREAM_PINS_MANIFESTS")
    if not raw:
        path = os.environ.get("DOWNSTREAM_PINS_FILE")
        raw = Path(path).read_text() if path else None
    if not raw:
        print("::warning::no downstream manifests configured (DOWNSTREAM_PINS_MANIFESTS / DOWNSTREAM_PINS_FILE)")
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict) or not all(
        isinstance(repo, str) and isinstance(paths, list) and all(isinstance(p, str) for p in paths)
        for repo, paths in data.items()
    ):
        sys.exit("::error::downstream manifests config must be a JSON object of repo -> [manifest paths]")
    return {repo: list(paths) for repo, paths in data.items()}


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("::error::no GitHub token in env (set GH_TOKEN)")
    return token


def _request(method: str, url: str, token: str, accept: str, data: dict | None = None):
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", accept)
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
    return raw if accept.endswith("raw+json") else json.loads(raw or b"null")


def fetch_manifest(repo: str, path: str, token: str) -> str:
    # ``raw+json`` returns the file bytes directly (no base64 round-trip).
    url = f"{API}/repos/{OWNER}/{repo}/contents/{path}"
    return _request("GET", url, token, "application/vnd.github.raw+json").decode("utf-8")


def iter_requirements(text: str) -> list[str]:
    """Every PEP 508 requirement string declared in the manifest."""
    data = tomllib.loads(text)
    out: list[str] = []

    def add(items: object) -> None:
        if isinstance(items, list):
            out.extend(item for item in items if isinstance(item, str))

    project = data.get("project", {})
    add(project.get("dependencies"))
    for group in project.get("optional-dependencies", {}).values():
        add(group)
    for group in data.get("dependency-groups", {}).values():
        add(group)
    add(data.get("tool", {}).get("uv", {}).get("dev-dependencies"))
    return out


def line_of(text: str, needle: str) -> int | None:
    for number, line in enumerate(text.splitlines(), start=1):
        if needle in line:
            return number
    return None


def current_core_versions() -> dict[str, str]:
    """Each CORE member's current released version, from the checked-out tree."""
    config = json.loads(Path("release-please-config.json").read_text())
    path_of = {entry.get("package-name"): path for path, entry in config["packages"].items()}
    versions: dict[str, str] = {}
    for package in CORE:
        path = path_of.get(package)
        if not path:
            print(f"::warning::{package} has no package-name in release-please-config.json")
            continue
        manifest = tomllib.loads((Path(path) / "pyproject.toml").read_text())
        versions[package] = manifest["project"]["version"]
    return versions


def resolve_targets() -> dict[str, str]:
    event = os.environ.get("GITHUB_EVENT_NAME", "")
    ref = os.environ.get("GITHUB_REF_NAME", "")
    if event == "push" and "-v" in ref:
        component, version = ref.rsplit("-v", 1)
        if component not in CORE:
            print(f"tag {ref} is not a CORE member; nothing to check.")
            return {}
        return {component: version}
    # workflow_dispatch (or a local run): sweep every CORE member at its current
    # released version, so a manual run is a full downstream freshness audit.
    return current_core_versions()


def find_violations(targets: dict[str, str], token: str, downstream: dict[str, list[str]]) -> dict[str, list[str]]:
    """title -> sorted, de-duplicated ``repo/file:line — range`` rows."""
    violations: dict[str, set[str]] = {}
    for repo, manifests in downstream.items():
        for path in manifests:
            try:
                text = fetch_manifest(repo, path, token)
            except urllib.error.HTTPError as error:
                # A private repo this token cannot read, or a manifest that moved.
                # Warn (never crash) so one unreachable downstream cannot mask the
                # rest of the sweep.
                print(f"::warning::could not read {repo}/{path}: HTTP {error.code} {error.reason}")
                continue
            for req_str in iter_requirements(text):
                try:
                    req = Requirement(req_str)
                except Exception:
                    # A malformed requirement line is the downstream's problem,
                    # not a reason to abort the sweep.
                    continue
                if req.name not in targets or not req.specifier:
                    continue
                version = targets[req.name]
                if Version(version) in req.specifier:
                    continue
                title = f"downstream pin excludes {req.name} {version}"
                row = f"- `{repo}` — `{path}`:{line_of(text, req_str)} — requires `{req.specifier}`"
                violations.setdefault(title, set()).add(row)
    return {title: sorted(rows) for title, rows in violations.items()}


def open_open_issues(token: str) -> dict[str, int]:
    """Exact title -> number for every OPEN issue (PRs excluded)."""
    issues: dict[str, int] = {}
    page = 1
    while True:
        url = f"{API}/repos/{OWNER}/{REPO}/issues?state=open&per_page=100&page={page}"
        batch = _request("GET", url, token, "application/vnd.github+json")
        if not batch:
            break
        for issue in batch:
            if "pull_request" not in issue:
                issues[issue["title"]] = issue["number"]
        if len(batch) < 100:
            break
        page += 1
    return issues


def upsert_issue(title: str, rows: list[str], token: str, existing: dict[str, int]) -> None:
    body = (
        "A CORE fleet member was released whose version falls OUTSIDE a downstream "
        "requirement range, so that downstream will not resolve the new release "
        "until its pin is widened.\n\n"
        "Stale pins:\n" + "\n".join(rows) + "\n\nWiden each listed range to admit the released version, then close "
        "this issue; a later run reopens a fresh one if anything is still stale.\n\n"
        "Opened by `.github/workflows/downstream-pins.yml`. This check never edits "
        "downstream repos — widening a downstream pin is the downstream's call."
    )
    if DRY_RUN:
        print(f"[dry-run] {title}\n{body}\n")
        return
    if title in existing:
        number = existing[title]
        _request(
            "PATCH",
            f"{API}/repos/{OWNER}/{REPO}/issues/{number}",
            token,
            "application/vnd.github+json",
            {"body": body},
        )
        print(f"updated issue #{number}: {title}")
    else:
        created = _request(
            "POST",
            f"{API}/repos/{OWNER}/{REPO}/issues",
            token,
            "application/vnd.github+json",
            {"title": title, "body": body},
        )
        print(f"opened issue #{created['number']}: {title}")


def main() -> int:
    token = _token()
    targets = resolve_targets()
    if not targets:
        return 0
    downstream = downstream_manifests()
    if not downstream:
        print("no downstream manifests configured; nothing to audit.")
        return 0
    print(f"checking downstream pins against released core version(s): {targets}")
    violations = find_violations(targets, token, downstream)
    if not violations:
        print("All downstream pins admit the released core version(s); nothing to do.")
        return 0
    existing = {} if DRY_RUN else open_open_issues(token)
    for title, rows in sorted(violations.items()):
        upsert_issue(title, rows, token, existing)
    # The issue is the durable signal; the run itself stays green so a release
    # wave is never blocked by a downstream's own stale pin.
    print(f"::warning::{len(violations)} stale downstream pin group(s); issue(s) opened/updated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
