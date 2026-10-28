"""Tests for scripts/release_label_check.py — which versions the pre-merge gate checks, and how.

The ``tai42-api-gate`` subprocess is stubbed throughout (the gate has its own suite).
The release-please projection is stubbed where a test is about the orchestration,
and run for real once, over a synthetic repository, to hold the contract between
this script and ``scripts/release-please/projection.js``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

import release_label_check as rlc  # importable via the scripts/ path conftest.py injects

REPO_ROOT = Path(__file__).resolve().parents[2]
RELEASE_PLEASE = REPO_ROOT / "scripts" / "release-please"
REPOSITORY = "acme/widgets"
TRAIN_BRANCH = "release-please--branches--main"
# The exact release-please version scripts/release-please pins and installs.
PINNED_RELEASE_PLEASE = json.loads((RELEASE_PLEASE / "package.json").read_text())["dependencies"]["release-please"]


def _pr(
    headline: str = "fix(kit): x (#7)",
    *,
    body: str = "",
    pr_body: str = "",
    head_ref: str = "topic",
    head_repository: str = REPOSITORY,
) -> rlc.PullRequest:
    return rlc.PullRequest(
        number=7,
        headline=headline,
        body=body,
        pr_body=pr_body,
        head_ref=head_ref,
        head_repository=head_repository,
        base_branch="main",
    )


MANIFEST = ".release-please-manifest.json"
CONFIG = "release-please-config.json"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Alice", "-c", "user.email=alice@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, message: str, files: dict[str, str]) -> str:
    for name, content in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(content)
    _git(repo, "add", "--all")
    _git(repo, "commit", "--quiet", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path, manifest: dict[str, str], packages: dict[str, dict[str, object]] | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main")
    packages = packages or {path: {"package-name": f"tai42-{Path(path).name}"} for path in manifest}
    config = {"release-type": "python", "include-component-in-tag": True, "packages": packages}
    _commit(repo, "chore: release main (#1)", {CONFIG: json.dumps(config, indent=2), MANIFEST: json.dumps(manifest)})
    return repo


def _packaged(root: Path, *dirs: str) -> None:
    """Give each dir a pyproject.toml under ``root`` so the check treats it as a packaged member."""
    for directory in dirs:
        (root / directory).mkdir(parents=True, exist_ok=True)
        (root / directory / "pyproject.toml").write_text("[project]\n")


def _squash_json(tmp_path: Path, *, merge: bool = False, rebase: bool = False, pull_request: object = None) -> Path:
    if pull_request is None:
        pull_request = {"viewerMergeHeadlineText": "fix(kit): x (#7)", "viewerMergeBodyText": "Body."}
    document = {
        "data": {
            "repository": {
                "squashMergeAllowed": True,
                "mergeCommitAllowed": merge,
                "rebaseMergeAllowed": rebase,
                "pullRequest": pull_request,
            }
        }
    }
    path = tmp_path / "squash.json"
    path.write_text(json.dumps(document))
    return path


# --- the release-train path -------------------------------------------------------------


def _train_repo(tmp_path: Path) -> tuple[Path, str]:
    repo = _repo(tmp_path, {"core/kit": "7.0.2", "core/cli": "11.0.2", "core/skeleton": "3.0.0"})
    base = _git(repo, "rev-parse", "HEAD")
    manifest = {"core/kit": "8.0.0", "core/cli": "11.0.2", "core/skeleton": "3.0.0", "plugins/agents": "1.0.0"}
    _commit(repo, "chore: release main (#9)", {MANIFEST: json.dumps(manifest)})
    return repo, base


def test_a_release_train_is_gated_at_its_manifest_versions(tmp_path) -> None:
    repo, base = _train_repo(tmp_path)

    gated = rlc.versions_to_gate(
        repo, base, "HEAD", _pr(head_ref=TRAIN_BRANCH), repository=REPOSITORY, release_please=RELEASE_PLEASE
    )

    assert gated == {
        "core/kit": ("8.0.0", "the release train's manifest"),
        "plugins/agents": ("1.0.0", "the release train's manifest"),
    }


def test_a_pull_request_that_adds_a_package_is_projected_not_taken_for_a_train(monkeypatch, tmp_path) -> None:
    # Adding a package edits the manifest; every package the pull request touches is still gated.
    repo, base = _train_repo(tmp_path)
    requests = _projected(
        monkeypatch,
        {
            "core/kit": {"component": "tai42-kit", "version": "7.0.3", "touched": True},
            "plugins/agents": {"component": "tai42-agents", "version": "1.1.0", "touched": True},
        },
        [MANIFEST, "core/kit/src/a.py", "plugins/agents/src/b.py"],
    )

    gated = rlc.versions_to_gate(repo, base, "HEAD", _pr(), repository=REPOSITORY, release_please=RELEASE_PLEASE)

    assert len(requests) == 1
    assert gated == {
        "core/kit": ("7.0.3", "projected by release-please 17.6.0"),
        "plugins/agents": ("1.1.0", "projected by release-please 17.6.0"),
    }


@pytest.mark.parametrize(
    ("head_ref", "head_repository"),
    [
        (TRAIN_BRANCH, "someone/widgets"),
        ("release-please--branches--develop", REPOSITORY),
        (f"{TRAIN_BRANCH}-copy", REPOSITORY),
        (f"{TRAIN_BRANCH}--components--tai42-kit", REPOSITORY),
    ],
)
def test_a_branch_that_only_looks_like_the_train_is_projected(monkeypatch, tmp_path, head_ref, head_repository) -> None:
    repo, base = _train_repo(tmp_path)
    requests = _projected(
        monkeypatch,
        {"core/kit": {"component": "tai42-kit", "version": "7.0.3", "touched": True}},
        [MANIFEST, "core/kit/src/a.py"],
    )
    pr = _pr(head_ref=head_ref, head_repository=head_repository)
    gated = rlc.versions_to_gate(repo, base, "HEAD", pr, repository=REPOSITORY, release_please=RELEASE_PLEASE)
    assert gated == {"core/kit": ("7.0.3", "projected by release-please 17.6.0")}
    assert len(requests) == 1


# --- the projection path ----------------------------------------------------------------


def _projected(monkeypatch, paths: dict[str, dict[str, object]], files: list[str]) -> list[dict[str, object]]:
    requests: list[dict[str, object]] = []

    real_run_node = rlc._run_node

    def fake_run_node(script: Path, request: dict[str, object]) -> Any:
        if script.name != "projection.js":
            return real_run_node(script, request)
        requests.append(request)
        return {"releasePlease": "17.6.0", "paths": paths}

    monkeypatch.setattr(rlc, "_run_node", fake_run_node)
    monkeypatch.setattr(rlc, "changed_files", lambda base, repo_root: files)
    return requests


def test_touched_packages_with_a_version_are_gated_at_the_projection(monkeypatch, tmp_path, capsys) -> None:
    repo, base = _train_repo(tmp_path)
    requests = _projected(
        monkeypatch,
        {
            "core/kit": {"component": "tai42-kit", "version": "8.0.0", "touched": True},
            "core/cli": {"component": "tai42-cli", "version": "11.0.3", "touched": False},
            "core/skeleton": {"component": "tai42-skeleton", "version": None, "touched": True},
        },
        ["core/kit/src/a.py", "core/skeleton/src/b.py"],
    )
    pr = _pr("feat(kit)!: x (#7)", body="Body.", pr_body="PR body")

    gated = rlc.versions_to_gate(repo, base, "HEAD", pr, repository=REPOSITORY, release_please=RELEASE_PLEASE)

    assert gated == {"core/kit": ("8.0.0", "projected by release-please 17.6.0")}
    assert requests == [
        {
            "repo": str(repo),
            "repository": REPOSITORY,
            "tree": "HEAD",
            "base": base,
            "pullRequest": {
                "number": 7,
                "headline": "feat(kit)!: x (#7)",
                "body": "Body.",
                "prBody": "PR body",
                "files": ["core/kit/src/a.py", "core/skeleton/src/b.py"],
            },
        }
    ]
    assert "core/cli projects 11.0.3, pending in the train, not touched by this pull request" in capsys.readouterr().out


def test_a_projection_failure_carries_its_stderr(tmp_path) -> None:
    failing = tmp_path / "failing.js"
    failing.write_text("process.stderr.write('no tag for core/kit'); process.exit(1);\n")
    with pytest.raises(SystemExit, match="no tag for core/kit"):
        rlc._run_node(failing, {"repo": str(tmp_path)})


def test_the_projection_is_fed_its_request_on_stdin(tmp_path) -> None:
    echo = tmp_path / "echo.js"
    echo.write_text(
        "let s='';process.stdin.on('data',c=>s+=c).on('end',()=>"
        "process.stdout.write(JSON.stringify({releasePlease:'x',paths:JSON.parse(s)})));\n"
    )
    assert rlc._run_node(echo, {"repo": "r"})["paths"] == {"repo": "r"}


@pytest.fixture
def fake_github(monkeypatch) -> Iterator[list[dict[str, object]]]:
    """A local GraphQL endpoint that associates no pull request with any commit."""
    requests: list[dict[str, object]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"authorization": self.headers["Authorization"], **payload})
            aliases = [alias for alias in payload["variables"] if alias[0] == "c"]
            commits = {alias: {"associatedPullRequests": {"nodes": []}} for alias in aliases}
            body = json.dumps({"data": {"repository": commits}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 the http.server signature
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GITHUB_GRAPHQL_URL", f"http://127.0.0.1:{server.server_address[1]}/graphql")
    yield requests
    server.shutdown()
    server.server_close()


def test_projection_runs_for_real_on_a_synthetic_repository(tmp_path, fake_github) -> None:
    if not (RELEASE_PLEASE / "node_modules").is_dir():
        pytest.fail("run npm ci in scripts/release-please")
    if shutil.which("node") is None:
        pytest.fail("node is required to run scripts/release-please/projection.js")
    repo = _repo(tmp_path, {"core/kit": "7.0.2", "core/cli": "11.0.2"})
    _git(repo, "tag", "tai42-kit-v7.0.2")
    _git(repo, "tag", "tai42-cli-v11.0.2")
    _commit(repo, "fix(cli): y (#5)", {"core/cli/src/y.py": "y\n"})
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, "Merge pull request into main", {"core/kit/src/a.py": "a\n"})
    pr = _pr("feat(kit)!: drop a method (#7)")

    gated = rlc.versions_to_gate(repo, base, "HEAD", pr, repository=REPOSITORY, release_please=RELEASE_PLEASE)

    assert gated == {"core/kit": ("8.0.0", f"projected by release-please {PINNED_RELEASE_PLEASE}")}
    assert fake_github
    assert all(request["authorization"] == "token test-token" for request in fake_github)


# --- the squash message GitHub will write -----------------------------------------------


def test_the_squash_text_is_read_from_the_graphql_response(tmp_path) -> None:
    assert rlc.read_squash(_squash_json(tmp_path)) == ("fix(kit): x (#7)", "Body.")


@pytest.mark.parametrize(
    "pull_request",
    [
        {"viewerMergeHeadlineText": "fix(kit): x (#7)"},
        {"viewerMergeBodyText": "Body."},
        {"viewerMergeHeadlineText": "", "viewerMergeBodyText": "Body."},
        {"viewerMergeHeadlineText": "fix(kit): x (#7)", "viewerMergeBodyText": None},
    ],
)
def test_an_incomplete_squash_response_is_refused(tmp_path, pull_request) -> None:
    with pytest.raises(SystemExit, match="::error::"):
        rlc.read_squash(_squash_json(tmp_path, pull_request=pull_request))


def test_a_missing_squash_response_is_refused(tmp_path) -> None:
    with pytest.raises(SystemExit, match="::error::"):
        rlc.read_squash(tmp_path / "absent.json")


def test_main_without_a_squash_response_is_refused(monkeypatch, tmp_path) -> None:
    argv = [
        "release_label_check.py",
        "--repo-root",
        str(tmp_path),
        "--base-ref",
        "abc123",
        "--pr-number",
        "7",
        "--repository",
        REPOSITORY,
        "--base-branch",
        "main",
        "--head-ref",
        "topic",
        "--head-repository",
        REPOSITORY,
    ]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit, match=r"::error::.*--squash-json"):
        rlc.main()


@pytest.mark.parametrize(("merge", "rebase"), [(True, False), (False, True), (True, True)])
def test_a_repository_that_allows_another_merge_method_is_refused(tmp_path, merge, rebase) -> None:
    with pytest.raises(SystemExit, match="::error::this check projects a squash merge"):
        rlc.read_squash(_squash_json(tmp_path, merge=merge, rebase=rebase))


# --- gating -----------------------------------------------------------------------------


def _record_gate(monkeypatch, code: int = 0) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []

    def fake_gate(package: str, directory: str, version: str, repo_root) -> tuple[int, str]:
        calls.append((package, directory, version))
        return code, f"{package}: gate output.\n"

    monkeypatch.setattr(rlc, "_run_gate", fake_gate)
    return calls


PACKAGES = {"core/kit": "tai42-kit", "plugins/agents": "tai42-agents", "core/cli": "tai42-cli"}


def test_check_gates_every_version_it_is_given(monkeypatch, tmp_path) -> None:
    _packaged(tmp_path, "core/kit", "plugins/agents", "core/cli")
    calls = _record_gate(monkeypatch)
    gated = {"core/kit": ("8.0.0", "label"), "plugins/agents": ("9.0.0", "label")}
    assert rlc.check(gated, PACKAGES, tmp_path) == 0
    assert sorted(calls) == [("tai42-agents", "plugins/agents", "9.0.0"), ("tai42-kit", "core/kit", "8.0.0")]


def test_check_fails_when_a_gate_refuses(monkeypatch, tmp_path, capsys) -> None:
    _packaged(tmp_path, "core/kit")
    _record_gate(monkeypatch, code=1)
    assert rlc.check({"core/kit": ("7.0.3", "label")}, PACKAGES, tmp_path) == 1
    assert "the PR body is not part of the squash message" in capsys.readouterr().out


def test_check_passes_when_nothing_is_gated(monkeypatch, tmp_path) -> None:
    calls = _record_gate(monkeypatch)
    assert rlc.check({}, PACKAGES, tmp_path) == 0
    assert calls == []


def test_check_skips_a_descriptor_only_package(monkeypatch, tmp_path) -> None:
    # A dir without a pyproject.toml is descriptor-only (no Python surface): the gate is
    # not run for it, but a packaged sibling in the same pull request still is.
    _packaged(tmp_path, "core/kit")
    (tmp_path / "plugins" / "connector-atlassian").mkdir(parents=True)
    calls = _record_gate(monkeypatch)
    packages = {**PACKAGES, "plugins/connector-atlassian": "tai42-connector-atlassian"}
    gated = {"core/kit": ("7.0.3", "label"), "plugins/connector-atlassian": ("2.0.13", "label")}
    assert rlc.check(gated, packages, tmp_path) == 0
    assert calls == [("tai42-kit", "core/kit", "7.0.3")]


def test_check_refuses_a_path_the_config_does_not_declare(monkeypatch, tmp_path) -> None:
    _record_gate(monkeypatch)
    with pytest.raises(SystemExit, match="::error::plugins/unknown"):
        rlc.check({"plugins/unknown": ("1.0.0", "label")}, PACKAGES, tmp_path)


def test_load_packages_maps_each_path_to_its_package_name(tmp_path) -> None:
    config = tmp_path / CONFIG
    config.write_text(json.dumps({"packages": {"core/kit": {"package-name": "tai42-kit", "exclude-paths": ["x"]}}}))
    assert rlc.load_packages(config) == {"core/kit": "tai42-kit"}


def test_load_packages_refuses_a_config_without_packages(tmp_path) -> None:
    config = tmp_path / CONFIG
    config.write_text("{}")
    with pytest.raises(SystemExit, match="declares no packages"):
        rlc.load_packages(config)
