#!/usr/bin/env python3
"""Check the fleet's downstream repos for a ``tai42-<core>`` pin that excludes a just-released core version.

Open a tracking issue ON the downstream repo whose manifest carries the excluding
pin — so that repo learns, at release time, that it will not resolve the new core
version until its own pin is widened.

The audited downstreams and the manifests to read are supplied by configuration
(``DOWNSTREAM_PINS_MANIFESTS`` JSON, or a ``DOWNSTREAM_PINS_FILE`` path to the
same); the CI workflow injects the map from a repository variable. The owner the
downstreams live under, and the repository whose workflow raised the issue, are
read from ``GITHUB_REPOSITORY`` (``owner/repo``), which GitHub Actions provides.
For each manifest we fetch it
over the GitHub contents API, parse every requirement out of
``[project].dependencies`` / ``[project.optional-dependencies]`` /
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

On a violation we open — or, deduped by exact title against that repo's own open
issues, update — an issue titled ``downstream pin excludes <pkg> <ver>`` on the
offending repo, listing its stale file/line/range rows. Only the tracking issue
is opened: widening the pin is the downstream maintainer's call. Pure Python
standard library + ``packaging``; the side effects are reading the file named by
``DOWNSTREAM_PINS_FILE`` when that variable is set, reading local files on a
manual dispatch (``release-please-config.json`` + each member's ``pyproject.toml``),
HTTPS reads of the manifests and of each offending repo's open issues, and the issue writes.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import Version

# The character set a GitHub repository name may use.
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]+")

API = "https://api.github.com"

# The CORE fleet members whose release can strand a downstream excluding pin.
# A tag outside this set (e.g. a plugin) is ignored by the trigger and here.
CORE = ("tai42-contract", "tai42-kit", "tai42-skeleton", "tai42-cli", "tai42-agents")

# Set to "1" to print the issues that WOULD be opened instead of writing them,
# to validate the checker without touching any tracker.
DRY_RUN = os.environ.get("DOWNSTREAM_PINS_DRY_RUN") == "1"


def repository() -> str:
    """The ``owner/repo`` of the repository this check runs in, from the Actions env."""
    value = os.environ.get("GITHUB_REPOSITORY", "")
    parts = value.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        sys.exit(
            "::error::GITHUB_REPOSITORY is not set to 'owner/repo'; "
            "cannot derive the owner the downstream repos live under"
        )
    return value


def downstream_manifests(self_repo: str) -> dict[str, list[str]]:
    """The downstream ``repo -> [manifest paths]`` map to audit, supplied by configuration.

    ``DOWNSTREAM_PINS_MANIFESTS`` carries the JSON map inline; ``DOWNSTREAM_PINS_FILE``
    points at a JSON file holding it (the inline var wins). The map drives issue
    WRITES, so it is validated here, the one chokepoint: every key is a bare
    repository name (``[A-Za-z0-9._-]+``, not ``.``/``..``) that is not ``self_repo``
    — issues open on the downstream repo, never the running one; every path is
    repo-relative with no leading ``/``, ``..``, or empty segment. Explicit, not
    discovered: the map is a reviewed configuration value, never scanned from a repo
    we were not told to read. Absent both vars the map is empty; the caller decides
    whether an empty map is an error for the event at hand.
    """
    config = _manifest_config()
    if config is None:
        return {}
    raw, source = config
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        sys.exit(f"::error::{source} is not valid JSON: {error}")
    if not isinstance(data, dict) or not all(
        isinstance(repo, str) and isinstance(paths, list) and all(isinstance(p, str) for p in paths)
        for repo, paths in data.items()
    ):
        sys.exit("::error::downstream manifests config must be a JSON object of repo -> [manifest paths]")
    _validate_manifest_map(data, self_repo)
    return {repo: list(paths) for repo, paths in data.items()}


def _manifest_config() -> tuple[str, str] | None:
    """The raw JSON config and the variable it came from, or ``None`` when nothing is configured.

    ``DOWNSTREAM_PINS_MANIFESTS`` carries the JSON inline and wins; ``DOWNSTREAM_PINS_FILE``
    points at a file holding it. An unreadable file path is a loud configuration error.
    """
    raw = os.environ.get("DOWNSTREAM_PINS_MANIFESTS")
    if raw:
        return raw, "DOWNSTREAM_PINS_MANIFESTS"
    path = os.environ.get("DOWNSTREAM_PINS_FILE")
    if not path:
        return None
    try:
        raw = Path(path).read_text()
    except OSError as error:
        sys.exit(f"::error::DOWNSTREAM_PINS_FILE path {path!r} could not be read: {error}")
    return (raw, "DOWNSTREAM_PINS_FILE") if raw else None


def _validate_manifest_map(data: dict[str, list[str]], self_repo: str) -> None:
    """Reject a map that would write to the running repo or read an unsafe manifest path."""
    seen: dict[str, str] = {}
    for repo, paths in data.items():
        if not _REPO_NAME.fullmatch(repo) or repo in {".", ".."}:
            sys.exit(f"::error::downstream repo key {repo!r} is not a valid repository name")
        # GitHub repository names are case-insensitive, so keys are compared case-folded:
        # a case-variant of the running repo still names it, and two case-variant keys
        # name one repository — auditing it twice would double-report.
        folded = repo.casefold()
        if folded == self_repo.casefold():
            sys.exit(
                f"::error::downstream map names the running repository {repo!r}; "
                "the pin issues must open on the downstream repo, never this one"
            )
        if folded in seen:
            sys.exit(
                f"::error::downstream map names {seen[folded]!r} and {repo!r}, "
                "which are the same repository (GitHub names are case-insensitive)"
            )
        seen[folded] = repo
        for p in paths:
            # Paths are echoed into workflow log lines, so a non-printable byte
            # (newline, tab, control char) is rejected before it can be logged.
            if not p.isprintable():
                sys.exit(f"::error::manifest path {p!r} for {repo!r} contains a non-printable character")
            if any(segment in {"", ".."} for segment in p.split("/")):
                sys.exit(
                    f"::error::manifest path {p!r} for {repo!r} must be repo-relative "
                    "(no leading '/', no '..' or empty segment)"
                )


def _token() -> str:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("::error::no GitHub token in env (set GH_TOKEN)")
    return token


def _request(method: str, url: str, token: str, accept: str, data: dict | None = None):
    body = None if data is None else json.dumps(data).encode("utf-8")
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310 fixed, trusted URL scheme
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", accept)
    request.add_header("X-GitHub-Api-Version", "2022-11-28")
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 fixed, trusted URL scheme
        raw = response.read()
    return raw if accept.endswith("raw+json") else json.loads(raw or b"null")


def fetch_manifest(owner: str, repo: str, path: str, token: str) -> str:
    """Fetch ``path`` from downstream ``owner/repo`` over the GitHub contents API, as text."""
    # ``raw+json`` returns the file bytes directly (no base64 round-trip). Each path
    # segment is percent-encoded, keeping ``/`` as the separator.
    quoted = "/".join(quote(segment, safe="") for segment in path.split("/"))
    url = f"{API}/repos/{owner}/{repo}/contents/{quoted}"
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
    """The 1-based line number of the first line containing ``needle``, or ``None``."""
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
            sys.exit(
                f"::error::{package} has no package-name entry in release-please-config.json; "
                "cannot determine its released version for the downstream sweep"
            )
        manifest_path = Path(path) / "pyproject.toml"
        try:
            manifest = tomllib.loads(manifest_path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as error:
            sys.exit(f"::error::cannot read {manifest_path} for {package}: {error}")
        try:
            versions[package] = manifest["project"]["version"]
        except KeyError:
            sys.exit(f"::error::{manifest_path} for {package} has no [project].version")
    return versions


def resolve_targets() -> dict[str, str]:
    """The ``pkg -> version`` pairs to check: the pushed component tag, or every core member on dispatch."""
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


def find_violations(
    owner: str, targets: dict[str, str], token: str, downstream: dict[str, list[str]]
) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    """Stale pins and the reads that failed, sweeping every manifest.

    Returns ``(violations, read_failures)`` where ``violations`` is
    ``repo -> {title -> sorted, de-duplicated rows}`` and ``read_failures`` names each
    ``repo/path`` whose manifest could not be read or parsed. An enumerated read/parse
    failure — an HTTP error, a network/URL error, a non-UTF-8 body, an unparsable TOML
    document, or an unparsable requirement string — is reported at its site as
    ``::error::`` and recorded, and the sweep continues so one such failure over one
    manifest does not mask the rest; the caller fails the run non-zero on any recorded
    failure. Any other exception is an unexpected fault in this script's own parsing
    path and propagates, ending the run with its traceback.
    """
    violations: dict[str, dict[str, set[str]]] = {}
    read_failures: list[str] = []
    for repo, manifests in downstream.items():
        for path in manifests:
            try:
                text = fetch_manifest(owner, repo, path, token)
                requirements = iter_requirements(text)
            except urllib.error.HTTPError as error:
                print(f"::error::could not read {repo}/{path}: HTTP {error.code} {error.reason}")
                read_failures.append(f"read {repo}/{path}")
                continue
            except (urllib.error.URLError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                print(f"::error::could not read {repo}/{path}: {error!r}")
                read_failures.append(f"read {repo}/{path}")
                continue
            for req_str in requirements:
                try:
                    req = Requirement(req_str)
                except InvalidRequirement as error:
                    print(f"::error::unparsable requirement in {repo}/{path}: {req_str!r} ({error})")
                    read_failures.append(f"read {repo}/{path}")
                    continue
                if req.name not in targets or not req.specifier:
                    continue
                version = targets[req.name]
                if Version(version) in req.specifier:
                    continue
                title = f"downstream pin excludes {req.name} {version}"
                line = line_of(text, req_str)
                location = f"`{path}`:{line}" if line is not None else f"`{path}`"
                row = f"- {location} — requires `{req.specifier}`"
                violations.setdefault(repo, {}).setdefault(title, set()).add(row)
    grouped = {repo: {title: sorted(rows) for title, rows in titles.items()} for repo, titles in violations.items()}
    return grouped, read_failures


def open_open_issues(owner: str, repo: str, token: str) -> dict[str, int]:
    """Exact title -> number for every OPEN issue on ``owner/repo`` (PRs excluded)."""
    issues: dict[str, int] = {}
    page = 1
    while True:
        url = f"{API}/repos/{owner}/{repo}/issues?state=open&per_page=100&page={page}"
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


def upsert_issue(
    owner: str,
    repo: str,
    origin: str,
    title: str,
    rows: list[str],
    token: str,
    existing: dict[str, int],
) -> None:
    """Open a tracking issue for ``title`` on ``owner/repo``, or update the existing one, from ``rows``."""
    body = (
        "A core fleet package this repository depends on was released at a version "
        "OUTSIDE one of its requirement ranges, so this repository will not resolve "
        "the new release until the pin is widened.\n\n"
        "Stale pins:\n" + "\n".join(rows) + "\n\nWiden each listed range to admit the released version, then close "
        "this issue; a later run reopens a fresh one if anything is still stale.\n\n"
        f"Opened by the downstream-pin check in `{origin}`. Only this tracking issue "
        "is opened here — widening the pin is this repository's maintainers' call."
    )
    if DRY_RUN:
        print(f"[dry-run] {repo}: {title}\n{body}\n")
        return
    if title in existing:
        number = existing[title]
        _request(
            "PATCH",
            f"{API}/repos/{owner}/{repo}/issues/{number}",
            token,
            "application/vnd.github+json",
            {"body": body},
        )
        print(f"updated {repo}#{number}: {title}")
    else:
        created = _request(
            "POST",
            f"{API}/repos/{owner}/{repo}/issues",
            token,
            "application/vnd.github+json",
            {"title": title, "body": body},
        )
        print(f"opened {repo}#{created['number']}: {title}")


def report_violations(
    owner: str,
    origin: str,
    violations: dict[str, dict[str, list[str]]],
    token: str,
) -> list[str]:
    """Open or update each repo's stale-pin issues; return one failure entry per repo/title not reported.

    Every repo and every title is attempted so one unreachable / issues-disabled /
    permission-denied repo, or one bad title, never masks the reporting for the rest;
    each failure is named at its site and returned for the caller's summary.
    """
    failures: list[str] = []
    for repo in sorted(violations):
        titles = violations[repo]
        try:
            existing = {} if DRY_RUN else open_open_issues(owner, repo, token)
        except Exception as error:
            # Broad by design: the run ends non-zero on any recorded failure, so an
            # odd response for one repo must not stop the reporting for the others.
            print(f"::error::failed to list open issues on {repo}: {error!r}")
            failures.append(f"list {repo}")
            continue
        for title in sorted(titles):
            try:
                upsert_issue(owner, repo, origin, title, titles[title], token, existing)
            except Exception as error:
                # Broad by design, as above.
                print(f"::error::failed to report `{title}` on {repo}: {error!r}")
                failures.append(f"write {repo}: {title}")
    return failures


def _dedupe(entries: list[str]) -> list[str]:
    """The entries with duplicates dropped, first-seen order preserved."""
    return list(dict.fromkeys(entries))


def main() -> int:
    """Run the downstream-pin sweep; return the process exit code."""
    token = _token()
    origin = repository()
    owner, self_repo = origin.split("/", 1)
    targets = resolve_targets()
    if not targets:
        return 0
    downstream = downstream_manifests(self_repo)
    if not downstream:
        # We are past the ``no targets`` guard, so released core version(s) were resolved
        # (a pushed component tag or a dispatch sweep) and a downstream pin audit IS
        # expected — an empty/unset manifest map here is a misconfiguration, not a benign
        # no-op. Fail LOUD (::error:: + nonzero) rather than silently skipping the audit and
        # letting a stale downstream pin slip through green.
        print(
            "::error::released core version(s) were resolved but no downstream manifests are configured "
            "(set DOWNSTREAM_PINS_MANIFESTS or DOWNSTREAM_PINS_FILE); refusing to skip the pin audit."
        )
        return 1
    print(f"checking downstream pins against released core version(s): {targets}")
    violations, failures = find_violations(owner, targets, token, downstream)
    failures = _dedupe(failures + report_violations(owner, origin, violations, token))
    if failures:
        print(f"::error::downstream-pin sweep failed for: {', '.join(failures)}")
        return 1
    if not violations:
        print("All downstream pins admit the released core version(s); nothing to do.")
        return 0
    # The issues are the durable signal; the check itself stays green on a stale
    # pin so a release is never blocked by a downstream's own pin.
    groups = sum(len(titles) for titles in violations.values())
    print(
        f"::warning::{groups} stale downstream pin group(s) across {len(violations)} repo(s); issue(s) opened/updated."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
