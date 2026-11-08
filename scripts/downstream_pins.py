#!/usr/bin/env python3
"""Check the fleet's downstream repos for a ``tai42-<core>`` pin that excludes a current core release.

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

Targets (which ``pkg==version`` pairs to test): every CORE member at its current
released version, read from ``release-please-config.json`` + each member's
``pyproject.toml`` on the checked-out tree, on every event. A release commit carries
every core member's current version, so each run is a full, convergent audit of the
tree it checked out. On a component-tag push (``tai42-<core>-v<ver>``) the tag is
cross-checked against that tree first: a ref that is not a CORE component tag, or a
tag whose version differs from the tree's version of that member, fails the run.

Each downstream repo's tracking issues are reconciled against the run: a violation
opens — or, deduped by exact title against that repo's own open issues, updates — an
issue titled ``downstream pin excludes <pkg> <ver>`` on the offending repo, listing
its stale file/line/range rows; every other open issue of that title shape whose
version this tree has reached (at or below the tree's version of that CORE package)
is stale, so the check comments why and closes it. An issue naming a newer version
than the tree's stays open, as does one whose package's live issue could not be
written this run. A repo whose manifest read failed this run keeps its open issues,
since its pins are unknown.
Only the tracking issues are written: widening the pin is the downstream maintainer's
call. Pure Python standard library + ``packaging``; the side effects are reading the
file named by ``DOWNSTREAM_PINS_FILE`` when that variable is set, reading local files
(``release-please-config.json`` + each member's ``pyproject.toml``), HTTPS reads of
the manifests and of each downstream repo's open issues, and the issue writes
(open, update, comment, close).
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
from typing import NamedTuple
from urllib.parse import quote

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version

# The character set a GitHub repository name may use.
_REPO_NAME = re.compile(r"[A-Za-z0-9._-]+")

API = "https://api.github.com"

# The CORE fleet members whose release can strand a downstream excluding pin.
# The workflow's tag filter lists exactly these; a pushed tag outside the set fails the run.
CORE = ("tai42-contract", "tai42-kit", "tai42-skeleton", "tai42-cli", "tai42-agents")

# The title of the tracking issue for one stale pin, and the pattern every such title
# matches; an open issue on a downstream repo that does not match is never touched.
_TRACKING_TITLE = "downstream pin excludes {package} {version}"
_TRACKING_TITLE_PATTERN = re.compile(
    r"^downstream pin excludes (" + "|".join(re.escape(package) for package in CORE) + r") (\S+)$"
)

# Set to "1" to print the issues that WOULD be opened or closed (with the closing
# comment) instead of writing them; the open-issue listing still runs, nothing else
# touches any tracker.
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
        if not paths:
            sys.exit(
                f"::error::downstream repo {repo!r} lists no manifest path to read; "
                "an empty list would audit no pin and close every tracking issue on it"
            )
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
    """The ``pkg -> version`` pairs to check: every CORE member at the checked-out tree's version.

    On a ``push`` the pushed ref must be a CORE component tag ``tai42-<core>-v<ver>``
    whose ``<ver>`` equals the tree's version of that member; anything else fails the
    run, so a tag on a commit carrying another version is never checked at the wrong
    version. Any other event (a manual dispatch, a local run) takes the tree as it is.
    """
    versions = current_core_versions()
    if os.environ.get("GITHUB_EVENT_NAME", "") == "push":
        _check_pushed_tag(os.environ.get("GITHUB_REF_NAME", ""), versions)
    return versions


def _check_pushed_tag(ref: str, versions: dict[str, str]) -> None:
    """Fail the run unless ``ref`` is a CORE component tag naming the tree's version of that member."""
    if "-v" not in ref:
        sys.exit(f"::error::pushed ref {ref!r} is not a component tag (tai42-<core>-v<version>)")
    component, version = ref.rsplit("-v", 1)
    if component not in CORE:
        sys.exit(
            f"::error::pushed tag {ref!r} names {component!r}, which is not a CORE member {CORE}; "
            "the workflow's tag filter and the checker's CORE set disagree"
        )
    if version != versions[component]:
        sys.exit(
            f"::error::pushed tag {ref!r} names {component} {version}, but the checked-out tree "
            f"carries {component} {versions[component]}"
        )


def read_failure_entry(repo: str, path: str) -> str:
    """The failure-summary entry naming a manifest of ``repo`` that could not be read or parsed."""
    return f"read {repo}/{path}"


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
                read_failures.append(read_failure_entry(repo, path))
                continue
            except (urllib.error.URLError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
                print(f"::error::could not read {repo}/{path}: {error!r}")
                read_failures.append(read_failure_entry(repo, path))
                continue
            for req_str in requirements:
                try:
                    req = Requirement(req_str)
                except InvalidRequirement as error:
                    print(f"::error::unparsable requirement in {repo}/{path}: {req_str!r} ({error})")
                    read_failures.append(read_failure_entry(repo, path))
                    continue
                if req.name not in targets or not req.specifier:
                    continue
                version = targets[req.name]
                if Version(version) in req.specifier:
                    continue
                title = _TRACKING_TITLE.format(package=req.name, version=version)
                line = line_of(text, req_str)
                location = f"`{path}`:{line}" if line is not None else f"`{path}`"
                row = f"- {location} — requires `{req.specifier}`"
                violations.setdefault(repo, {}).setdefault(title, set()).add(row)
    grouped = {repo: {title: sorted(rows) for title, rows in titles.items()} for repo, titles in violations.items()}
    return grouped, read_failures


def list_open_issues(owner: str, repo: str, token: str) -> dict[str, int]:
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
) -> int | None:
    """Open a tracking issue for ``title`` on ``owner/repo``, or update the existing one, from ``rows``.

    Returns the issue's number; ``None`` for an issue a dry run would open.
    """
    body = (
        "A core fleet package this repository depends on was released at a version "
        "OUTSIDE one of its requirement ranges, so this repository will not resolve "
        "the new release until the pin is widened.\n\n"
        "Stale pins:\n" + "\n".join(rows) + "\n\nWiden each listed range to admit the released version. "
        "The check closes this issue itself on its next run (the next core release, or a manual "
        "dispatch of its workflow) once this repository's pins admit the current releases.\n\n"
        f"Opened by the downstream-pin check in `{origin}`. Only this tracking issue "
        "is opened here — widening the pin is this repository's maintainers' call."
    )
    if DRY_RUN:
        print(f"[dry-run] {repo}: {title}\n{body}\n")
        return existing.get(title)
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
        return number
    created = _request(
        "POST",
        f"{API}/repos/{owner}/{repo}/issues",
        token,
        "application/vnd.github+json",
        {"title": title, "body": body},
    )
    print(f"opened {repo}#{created['number']}: {title}")
    return created["number"]


def closing_comment(origin: str, package: str, current: str, tracked_by: str | None) -> str:
    """Why a stale tracking issue for ``package`` is closed; ``tracked_by`` names the live issue, if any."""
    if tracked_by is None:
        return (
            f"Closed by the downstream-pin check in `{origin}`: the current {package} release is "
            f"{current} and this repository's pins admit it."
        )
    return (
        f"Closed by the downstream-pin check in `{origin}`: the current {package} release is "
        f"{current}; its stale pin is tracked by {tracked_by}."
    )


def close_issue(owner: str, repo: str, number: int, title: str, comment: str, token: str) -> None:
    """Comment ``comment`` on issue ``number`` of ``owner/repo``, then close it as completed."""
    if DRY_RUN:
        print(f"[dry-run] {repo}: would close #{number}: {title}\n{comment}\n")
        return
    issue_url = f"{API}/repos/{owner}/{repo}/issues/{number}"
    _request("POST", f"{issue_url}/comments", token, "application/vnd.github+json", {"body": comment})
    _request(
        "PATCH",
        issue_url,
        token,
        "application/vnd.github+json",
        {"state": "closed", "state_reason": "completed"},
    )
    print(f"closed {repo}#{number}: {title}")


class TrackingTitle(NamedTuple):
    """The CORE package and the version a tracking-issue title names."""

    package: str
    version: Version


def parse_tracking_title(title: str) -> TrackingTitle | None:
    """The package and version ``title`` names, or ``None`` when it is not a tracking title.

    A title outside ``_TRACKING_TITLE_PATTERN``, or one whose version part is not a valid
    version, is not a tracking title this check owns.
    """
    match = _TRACKING_TITLE_PATTERN.fullmatch(title)
    if match is None:
        return None
    try:
        version = Version(match.group(2))
    except InvalidVersion:
        return None
    return TrackingTitle(match.group(1), version)


class LiveIssues(NamedTuple):
    """What writing a repo's live tracking titles left behind."""

    failures: list[str]  # one entry per title that could not be written
    tracked_by: dict[str, str]  # package -> the reference to its live issue
    unwritten: set[str]  # packages whose live issue could not be written


def reconcile_repo(
    owner: str,
    repo: str,
    origin: str,
    targets: dict[str, str],
    titles: dict[str, list[str]],
    token: str,
    *,
    pins_known: bool,
) -> tuple[list[str], int]:
    """Bring ``repo``'s tracking issues in line with this run's live ``titles``.

    Lists the repo's open issues once, opens or updates one issue per live title, then
    closes the stale ones (``close_stale_issues``). Nothing is closed when ``pins_known``
    is false: a manifest read failed, so the live set is incomplete. Returns one failure
    entry per listing, title or close that failed, and the number of issues closed.
    """
    try:
        existing = list_open_issues(owner, repo, token)
    except Exception as error:
        # Broad by design: the run ends non-zero on any recorded failure, so an
        # odd response for one repo must not stop the reconciliation of the others.
        print(f"::error::failed to list open issues on {repo}: {error!r}")
        return [f"list {repo}"], 0
    live = report_live_titles(owner, repo, origin, titles, token, existing)
    if not pins_known:
        print(f"{repo}: stale tracking issues left open; a manifest read failed this run, so its pins are unknown")
        return live.failures, 0
    close_failures, closed = close_stale_issues(owner, repo, origin, targets, titles, live, token, existing)
    return live.failures + close_failures, closed


def report_live_titles(
    owner: str,
    repo: str,
    origin: str,
    titles: dict[str, list[str]],
    token: str,
    existing: dict[str, int],
) -> LiveIssues:
    """Open or update one issue on ``repo`` per live title; every title is attempted."""
    failures: list[str] = []
    tracked_by: dict[str, str] = {}
    unwritten: set[str] = set()
    for title in sorted(titles):
        live = parse_tracking_title(title)
        if live is None:
            raise ValueError(f"{title!r} is not a tracking-issue title")
        try:
            number = upsert_issue(owner, repo, origin, title, titles[title], token, existing)
        except Exception as error:
            # Broad by design, as in ``reconcile_repo``.
            print(f"::error::failed to report `{title}` on {repo}: {error!r}")
            failures.append(f"write {repo}: {title}")
            unwritten.add(live.package)
            continue
        tracked_by[live.package] = (
            f"#{number}" if number is not None else f"the issue `{title}` this dry run would open"
        )
    return LiveIssues(failures, tracked_by, unwritten)


def close_stale_issues(
    owner: str,
    repo: str,
    origin: str,
    targets: dict[str, str],
    titles: dict[str, list[str]],
    live: LiveIssues,
    token: str,
    existing: dict[str, int],
) -> tuple[list[str], int]:
    """Close every open tracking issue on ``repo`` that is not live and this tree has reached.

    An open issue is closed when ``parse_tracking_title`` accepts its title, the title is
    not in the live ``titles``, and its version is at or below this tree's version of its
    package. It stays open when its package's live issue could not be written (it is then
    the only tracker), or when it names a version newer than this tree's (the tree
    predates that release, so it cannot judge it). Returns one failure entry per close
    that failed, and the number closed; every close is attempted.
    """
    failures: list[str] = []
    closed = 0
    for title, number in sorted(existing.items()):
        tracked = parse_tracking_title(title)
        if tracked is None or title in titles:
            continue
        package = tracked.package
        if package in live.unwritten:
            print(f"{repo}: #{number} left open; the live {package} issue could not be written this run")
            continue
        current = targets[package]
        if tracked.version > Version(current):
            print(
                f"{repo}: #{number} left open; it names {package} {tracked.version}, newer than this tree's {current}"
            )
            continue
        comment = closing_comment(origin, package, current, live.tracked_by.get(package))
        try:
            close_issue(owner, repo, number, title, comment, token)
        except Exception as error:
            # Broad by design, as in ``reconcile_repo``.
            print(f"::error::failed to close #{number} `{title}` on {repo}: {error!r}")
            failures.append(f"close {repo}: {title}")
            continue
        closed += 1
    return failures, closed


def reconcile_issues(
    owner: str,
    origin: str,
    targets: dict[str, str],
    violations: dict[str, dict[str, list[str]]],
    repos: list[str],
    unread_repos: set[str],
    token: str,
) -> tuple[list[str], int]:
    """Reconcile the tracking issues of every downstream repo in ``repos``.

    Returns the failure entries of every repo and the total number of issues closed;
    a failure on one repo never stops the others.
    """
    failures: list[str] = []
    closed = 0
    for repo in sorted(repos):
        repo_failures, repo_closed = reconcile_repo(
            owner,
            repo,
            origin,
            targets,
            violations.get(repo, {}),
            token,
            pins_known=repo not in unread_repos,
        )
        failures.extend(repo_failures)
        closed += repo_closed
    return failures, closed


def _dedupe(entries: list[str]) -> list[str]:
    """The entries with duplicates dropped, first-seen order preserved."""
    return list(dict.fromkeys(entries))


def main() -> int:
    """Run the downstream-pin sweep; return the process exit code."""
    token = _token()
    origin = repository()
    owner, self_repo = origin.split("/", 1)
    targets = resolve_targets()
    downstream = downstream_manifests(self_repo)
    if not downstream:
        # Every event resolves every CORE member's version, so a downstream pin audit IS
        # expected: an empty/unset manifest map is a misconfiguration, never a benign no-op.
        print(
            "::error::released core version(s) were resolved but no downstream manifests are configured "
            "(set DOWNSTREAM_PINS_MANIFESTS or DOWNSTREAM_PINS_FILE); refusing to skip the pin audit."
        )
        return 1
    print(f"checking downstream pins against released core version(s): {targets}")
    violations, read_failures = find_violations(owner, targets, token, downstream)
    unread_repos = {
        repo
        for repo, paths in downstream.items()
        if any(read_failure_entry(repo, path) in read_failures for path in paths)
    }
    write_failures, closed = reconcile_issues(owner, origin, targets, violations, list(downstream), unread_repos, token)
    if closed:
        print(f"{'would close' if DRY_RUN else 'closed'} {closed} stale tracking issue(s).")
    failures = _dedupe(read_failures + write_failures)
    if failures:
        print(f"::error::downstream-pin sweep failed for: {', '.join(failures)}")
        return 1
    if not violations:
        print("All downstream pins admit the released core version(s); no stale pin to report.")
        return 0
    # The issues are the durable signal; the check itself stays green on a stale
    # pin so a release is never blocked by a downstream's own pin.
    groups = sum(len(titles) for titles in violations.values())
    action = "would be opened/updated" if DRY_RUN else "opened/updated"
    print(f"::warning::{groups} stale downstream pin group(s) across {len(violations)} repo(s); issue(s) {action}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
