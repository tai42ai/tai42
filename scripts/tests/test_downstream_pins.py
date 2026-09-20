"""Unit tests for scripts/downstream_pins.py — the downstream-pin tracking-issue opener.

Hermetic: every GitHub call is faked at the ``_request`` seam, so no network runs.
Fixtures are neutral (``acme-app`` / ``plugins/alpha/pyproject.toml``).
"""

from __future__ import annotations

import urllib.error
from urllib.parse import parse_qs, urlparse

import pytest

import downstream_pins as dp  # importable via the scripts/ path conftest.py injects

RUNNING_REPO = "acme-org/hub"  # GITHUB_REPOSITORY of the repo the check runs in
OWNER = "acme-org"


def _manifest(spec: str) -> str:
    """A synthetic downstream manifest carrying one CORE pin at ``spec``."""
    return f'[project]\nname = "acme-app"\ndependencies = [\n  "tai42-contract{spec}",\n  "requests>=2",\n]\n'


class FakeGitHub:
    """Routes ``_request`` calls to canned contents/issues responses and records writes."""

    def __init__(
        self,
        manifests: dict[str, str | bytes | BaseException],
        issue_pages: dict[str, list[list[dict]]] | None = None,
        write_error_repos: set[str] = frozenset(),
        fail_write_titles: set[str] = frozenset(),
        list_error_repos: set[str] = frozenset(),
    ) -> None:
        # manifests: url -> file text (str), raw bytes, or an exception instance to raise;
        # issue_pages: repo -> [page1_issues, page2_issues, ...];
        # list_error_repos: repos whose open-issues listing GET raises.
        self.manifests = manifests
        self.issue_pages = issue_pages or {}
        self.write_error_repos = set(write_error_repos)
        self.fail_write_titles = set(fail_write_titles)
        self.list_error_repos = set(list_error_repos)
        self.calls: list[tuple[str, str, dict | None]] = []
        self._next_number = 1000

    def request(self, method: str, url: str, token: str, accept: str, data: dict | None = None):
        self.calls.append((method, url, data))
        if "/contents/" in url:
            value = self.manifests[url]
            if isinstance(value, BaseException):
                raise value
            return value if isinstance(value, bytes) else value.encode("utf-8")
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")  # repos / owner / repo / issues [/ number]
        repo = parts[2]
        if method == "GET":
            if repo in self.list_error_repos:
                raise urllib.error.HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)
            page = int(parse_qs(parsed.query)["page"][0])
            pages = self.issue_pages.get(repo, [[]])
            return pages[page - 1] if page - 1 < len(pages) else []
        if repo in self.write_error_repos:
            raise urllib.error.HTTPError(url, 410, "Issues are disabled", hdrs=None, fp=None)
        if method == "POST" and data and data.get("title") in self.fail_write_titles:
            raise urllib.error.HTTPError(url, 422, "Validation failed", hdrs=None, fp=None)
        if method == "POST":
            self._next_number += 1
            return {"number": self._next_number}
        return {}  # PATCH

    def writes(self, method: str) -> list[tuple[str, dict | None]]:
        return [(url, data) for m, url, data in self.calls if m == method]

    def fetches(self) -> list[str]:
        return [url for m, url, _ in self.calls if "/contents/" in url]


def _contents_url(repo: str, path: str) -> str:
    return f"{dp.API}/repos/{OWNER}/{repo}/contents/{path}"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", RUNNING_REPO)
    monkeypatch.setenv("GH_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "push")
    monkeypatch.setenv("GITHUB_REF_NAME", "tai42-contract-v2.0.0")
    # Clear the manifest-config vars so a host value can never leak into a test;
    # each test that needs them sets its own.
    monkeypatch.delenv("DOWNSTREAM_PINS_MANIFESTS", raising=False)
    monkeypatch.delenv("DOWNSTREAM_PINS_FILE", raising=False)
    monkeypatch.delenv("DOWNSTREAM_PINS_DRY_RUN", raising=False)
    monkeypatch.setattr(dp, "DRY_RUN", False)


def _run_main(monkeypatch: pytest.MonkeyPatch, fake: FakeGitHub, manifest_map: dict[str, list[str]]) -> int:
    import json

    monkeypatch.setattr(dp, "_request", fake.request)
    monkeypatch.setenv("DOWNSTREAM_PINS_MANIFESTS", json.dumps(manifest_map))
    return dp.main()


# --- owner derivation -------------------------------------------------------


def test_owner_derived_from_github_repository() -> None:
    assert dp.repository() == RUNNING_REPO
    assert dp.repository().split("/", 1)[0] == OWNER


def test_missing_github_repository_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    with pytest.raises(SystemExit) as exc:
        dp.repository()
    assert "GITHUB_REPOSITORY" in str(exc.value)


def test_github_repository_without_slash_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "no-owner")
    with pytest.raises(SystemExit):
        dp.repository()


@pytest.mark.parametrize("value", ["/hub", "acme-org/", "/", "acme-org/hub/extra"])
def test_github_repository_with_empty_half_fails_loudly(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", value)
    with pytest.raises(SystemExit):
        dp.repository()


# --- config validation ------------------------------------------------------


def test_map_naming_the_running_repo_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub({})
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, fake, {"hub": ["pyproject.toml"]})
    assert "hub" in str(exc.value)
    assert fake.fetches() == []  # nothing read
    assert fake.writes("POST") == []  # nothing written
    assert fake.writes("PATCH") == []


@pytest.mark.parametrize("bad_repo", ["other-org/repo", "..", ".", "a/b"])
def test_invalid_repo_key_is_config_error(monkeypatch: pytest.MonkeyPatch, bad_repo: str) -> None:
    fake = FakeGitHub({})
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, fake, {bad_repo: ["pyproject.toml"]})
    assert bad_repo in str(exc.value)
    assert fake.fetches() == []


@pytest.mark.parametrize("bad_path", ["../secret.toml", "/etc/passwd", "a/../b.toml", "sub/", ""])
def test_unsafe_manifest_path_is_config_error(monkeypatch: pytest.MonkeyPatch, bad_path: str) -> None:
    fake = FakeGitHub({})
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, fake, {"acme-app": [bad_path]})
    assert "acme-app" in str(exc.value)
    assert fake.fetches() == []


def test_manifest_path_is_url_quoted_per_segment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, str] = {}

    def fake_request(method, url, token, accept, data=None):
        seen["url"] = url
        return b"[project]\n"

    monkeypatch.setattr(dp, "_request", fake_request)
    dp.fetch_manifest("acme-org", "acme-app", "plugins/a b/pyproject.toml", "t0ken")
    assert seen["url"] == f"{dp.API}/repos/acme-org/acme-app/contents/plugins/a%20b/pyproject.toml"


# --- grouping ---------------------------------------------------------------


def test_rows_grouped_per_downstream_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(
        {
            _contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5,<2"),
            _contents_url("alpha", "plugins/alpha/pyproject.toml"): _manifest(">=1.0,<2"),
        }
    )
    monkeypatch.setattr(dp, "_request", fake.request)
    violations, failures = dp.find_violations(
        OWNER,
        {"tai42-contract": "2.0.0"},
        "t0ken",
        {"acme-app": ["pyproject.toml"], "alpha": ["plugins/alpha/pyproject.toml"]},
    )
    assert failures == []
    assert set(violations) == {"acme-app", "alpha"}
    assert violations["acme-app"] == {
        "downstream pin excludes tai42-contract 2.0.0": ["- `pyproject.toml`:4 — requires `<2,>=1.5`"]
    }
    assert violations["alpha"]["downstream pin excludes tai42-contract 2.0.0"] == [
        "- `plugins/alpha/pyproject.toml`:4 — requires `<2,>=1.0`"
    ]


def test_admitting_pin_yields_no_violation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub({_contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5")})
    monkeypatch.setattr(dp, "_request", fake.request)
    violations, failures = dp.find_violations(
        OWNER, {"tai42-contract": "2.0.0"}, "t0ken", {"acme-app": ["pyproject.toml"]}
    )
    assert violations == {}
    assert failures == []


def test_row_without_a_matched_line_has_no_none_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``<`` is ``<`` after TOML decoding, so the parsed requirement string never
    # appears literally in any source line and ``line_of`` returns None.
    text = '[project]\nname = "acme-app"\ndependencies = [\n  "tai42-contract>=1.5,\\u003c2",\n]\n'
    fake = FakeGitHub({_contents_url("acme-app", "pyproject.toml"): text})
    monkeypatch.setattr(dp, "_request", fake.request)
    violations, failures = dp.find_violations(
        OWNER, {"tai42-contract": "2.0.0"}, "t0ken", {"acme-app": ["pyproject.toml"]}
    )
    assert failures == []
    rows = violations["acme-app"]["downstream pin excludes tai42-contract 2.0.0"]
    assert rows == ["- `pyproject.toml` — requires `<2,>=1.5`"]
    assert ":None" not in rows[0]


# --- read / parse failures --------------------------------------------------


def test_http_read_failure_recorded_and_other_repos_still_audited(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        {
            _contents_url("acme-app", "pyproject.toml"): urllib.error.HTTPError(
                _contents_url("acme-app", "pyproject.toml"), 404, "Not Found", hdrs=None, fp=None
            ),
            _contents_url("beta", "pyproject.toml"): _manifest(">=1.5,<2"),
        }
    )
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"], "beta": ["pyproject.toml"]})
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error::" in out
    assert "acme-app/pyproject.toml" in out
    # The healthy repo was still audited AND its violation still reported.
    assert any(url == f"{dp.API}/repos/{OWNER}/beta/issues" for url, _ in fake.writes("POST"))


@pytest.mark.parametrize(
    "value",
    [
        urllib.error.URLError("timed out"),
        b"\xff\xfe not utf-8",  # decode failure
        "this is not = valid toml [",  # TOML parse failure
    ],
)
def test_manifest_read_or_parse_failure_recorded(monkeypatch: pytest.MonkeyPatch, value) -> None:
    fake = FakeGitHub({_contents_url("acme-app", "pyproject.toml"): value})
    monkeypatch.setattr(dp, "_request", fake.request)
    violations, failures = dp.find_violations(
        OWNER, {"tai42-contract": "2.0.0"}, "t0ken", {"acme-app": ["pyproject.toml"]}
    )
    assert violations == {}
    assert failures == ["read acme-app/pyproject.toml"]


def test_unparsable_requirement_recorded_and_sweep_continues(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    text = (
        '[project]\nname = "acme-app"\n'
        'dependencies = [\n  "=== not a requirement ===",\n  "tai42-contract>=1.5,<2",\n]\n'
    )
    fake = FakeGitHub({_contents_url("acme-app", "pyproject.toml"): text})
    monkeypatch.setattr(dp, "_request", fake.request)
    violations, failures = dp.find_violations(
        OWNER, {"tai42-contract": "2.0.0"}, "t0ken", {"acme-app": ["pyproject.toml"]}
    )
    out = capsys.readouterr().out
    assert failures == ["read acme-app/pyproject.toml"]
    assert "=== not a requirement ===" in out
    # The valid excluding pin in the same file is still found.
    assert violations["acme-app"] == {
        "downstream pin excludes tai42-contract 2.0.0": ["- `pyproject.toml`:5 — requires `<2,>=1.5`"]
    }


def test_read_failure_exits_nonzero_but_reports_found_violations(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        {
            _contents_url("acme-app", "pyproject.toml"): urllib.error.URLError("timed out"),
            _contents_url("beta", "pyproject.toml"): _manifest(">=1.5,<2"),
        }
    )
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"], "beta": ["pyproject.toml"]})
    out = capsys.readouterr().out
    assert rc == 1
    assert "acme-app/pyproject.toml" in out
    assert len(fake.writes("POST")) == 1  # beta's violation was reported


# --- write routing ----------------------------------------------------------


def test_issue_posted_to_owning_repo_never_the_running_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub({_contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5,<2")})
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"]})
    assert rc == 0
    posts = fake.writes("POST")
    assert len(posts) == 1
    url, data = posts[0]
    assert url == f"{dp.API}/repos/{OWNER}/acme-app/issues"
    assert data["title"] == "downstream pin excludes tai42-contract 2.0.0"
    # Nothing is written to (or read from the issues of) the repo the check runs in.
    assert all(f"/repos/{RUNNING_REPO}/issues" not in url for _, url, _ in fake.calls)


def test_body_carries_rows_and_widen_and_close(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub({_contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5,<2")})
    _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"]})
    body = fake.writes("POST")[0][1]["body"]
    assert "- `pyproject.toml`:4 — requires `<2,>=1.5`" in body
    assert "Widen each listed range to admit the released version, then close" in body
    assert f"Opened by the downstream-pin check in `{RUNNING_REPO}`" in body


def test_existing_open_issue_is_patched_not_duplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    title = "downstream pin excludes tai42-contract 2.0.0"
    fake = FakeGitHub(
        {_contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5,<2")},
        issue_pages={"acme-app": [[{"title": title, "number": 7}]]},
    )
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"]})
    assert rc == 0
    assert fake.writes("POST") == []
    patches = fake.writes("PATCH")
    assert len(patches) == 1
    assert patches[0][0] == f"{dp.API}/repos/{OWNER}/acme-app/issues/7"


def test_same_title_on_two_repos_yields_two_independent_upserts(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeGitHub(
        {
            _contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5,<2"),
            _contents_url("beta", "pyproject.toml"): _manifest(">=1.5,<2"),
        }
    )
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"], "beta": ["pyproject.toml"]})
    assert rc == 0
    post_urls = sorted(url for url, _ in fake.writes("POST"))
    assert post_urls == [
        f"{dp.API}/repos/{OWNER}/acme-app/issues",
        f"{dp.API}/repos/{OWNER}/beta/issues",
    ]


def test_pull_requests_in_issue_listing_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    title = "downstream pin excludes tai42-contract 2.0.0"
    fake = FakeGitHub(
        {_contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5,<2")},
        # A PR carrying the exact title must NOT be treated as the existing issue.
        issue_pages={"acme-app": [[{"title": title, "number": 3, "pull_request": {"url": "..."}}]]},
    )
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"]})
    assert rc == 0
    assert len(fake.writes("POST")) == 1
    assert fake.writes("PATCH") == []


def test_open_issue_listing_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    page1 = [{"title": f"other-{i}", "number": i} for i in range(100)]
    page2 = [{"title": "downstream pin excludes tai42-contract 2.0.0", "number": 500}]
    fake = FakeGitHub({}, issue_pages={"acme-app": [page1, page2]})
    monkeypatch.setattr(dp, "_request", fake.request)
    found = dp.open_open_issues(OWNER, "acme-app", "t0ken")
    assert len(found) == 101
    assert found["downstream pin excludes tai42-contract 2.0.0"] == 500
    requested_pages = [parse_qs(urlparse(url).query)["page"][0] for _, url, _ in fake.calls]
    assert requested_pages == ["1", "2"]  # stops once a short page returns


# --- dry run ----------------------------------------------------------------


def test_dry_run_writes_nothing(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(dp, "DRY_RUN", True)
    fake = FakeGitHub({_contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5,<2")})
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"]})
    assert rc == 0
    assert fake.writes("POST") == []
    assert fake.writes("PATCH") == []
    out = capsys.readouterr().out
    assert "[dry-run] acme-app: downstream pin excludes tai42-contract 2.0.0" in out


# --- write errors -----------------------------------------------------------


def test_write_error_exits_nonzero_naming_the_repo(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        {
            _contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5,<2"),
            _contents_url("beta", "pyproject.toml"): _manifest(">=1.5,<2"),
        },
        write_error_repos={"beta"},
    )
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"], "beta": ["pyproject.toml"]})
    assert rc == 1
    err = capsys.readouterr().out
    assert "on beta" in err
    assert "downstream-pin sweep failed for" in err
    assert "beta" in err
    # The healthy repo was still reported despite beta's failure.
    assert any(url == f"{dp.API}/repos/{OWNER}/acme-app/issues" for url, _ in fake.writes("POST"))


def test_second_title_still_attempted_after_first_title_write_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # One repo, two stale pins (two titles): the first title's write fails, the
    # second must still be attempted.
    monkeypatch.setattr(dp, "resolve_targets", lambda: {"tai42-contract": "2.0.0", "tai42-kit": "3.0.0"})
    manifest = '[project]\nname = "acme-app"\ndependencies = [\n  "tai42-contract>=1.5,<2",\n  "tai42-kit>=2,<3",\n]\n'
    fake = FakeGitHub(
        {_contents_url("acme-app", "pyproject.toml"): manifest},
        fail_write_titles={"downstream pin excludes tai42-contract 2.0.0"},
    )
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"]})
    out = capsys.readouterr().out
    assert rc == 1
    assert "downstream pin excludes tai42-contract 2.0.0" in out  # the failing title named
    posted_titles = {data["title"] for _, data in fake.writes("POST")}
    assert "downstream pin excludes tai42-kit 3.0.0" in posted_titles  # second title still attempted


# --- config validation: case-insensitive repo keys --------------------------


def test_case_variant_of_running_repo_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # GitHub repo names are case-insensitive, so "Hub" IS the running "hub":
    # it must be rejected before any read or write, exactly like an exact match.
    fake = FakeGitHub({})
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, fake, {"Hub": ["pyproject.toml"]})
    assert "Hub" in str(exc.value)
    assert fake.fetches() == []
    assert fake.writes("POST") == []
    assert fake.writes("PATCH") == []


def test_two_case_variant_keys_are_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two keys differing only in case name ONE repository; auditing it twice would
    # double-report, so it is a configuration error naming both keys — no reads.
    fake = FakeGitHub({})
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"], "Acme-App": ["a.toml"]})
    message = str(exc.value)
    assert "acme-app" in message
    assert "Acme-App" in message
    assert fake.fetches() == []
    assert fake.writes("POST") == []


# --- config validation: non-printable manifest paths ------------------------


@pytest.mark.parametrize("bad_path", ["plugins/a\nb.toml", "a\tb.toml", "x\x00y.toml", "sub/\rfile.toml"])
def test_nonprintable_manifest_path_is_config_error(monkeypatch: pytest.MonkeyPatch, bad_path: str) -> None:
    fake = FakeGitHub({})
    with pytest.raises(SystemExit) as exc:
        _run_main(monkeypatch, fake, {"acme-app": [bad_path]})
    assert "acme-app" in str(exc.value)
    assert fake.fetches() == []


# --- config validation: malformed / missing manifest config -----------------


def test_malformed_inline_manifests_json_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOWNSTREAM_PINS_MANIFESTS", "{not valid json")
    with pytest.raises(SystemExit) as exc:
        dp.downstream_manifests("hub")
    assert "DOWNSTREAM_PINS_MANIFESTS" in str(exc.value)


def test_malformed_manifests_file_json_is_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    path = tmp_path / "manifests.json"
    path.write_text("{not valid json")
    monkeypatch.delenv("DOWNSTREAM_PINS_MANIFESTS", raising=False)
    monkeypatch.setenv("DOWNSTREAM_PINS_FILE", str(path))
    with pytest.raises(SystemExit) as exc:
        dp.downstream_manifests("hub")
    assert "DOWNSTREAM_PINS_FILE" in str(exc.value)


def test_missing_manifests_file_is_config_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("DOWNSTREAM_PINS_MANIFESTS", raising=False)
    monkeypatch.setenv("DOWNSTREAM_PINS_FILE", str(tmp_path / "does-not-exist.json"))
    with pytest.raises(SystemExit) as exc:
        dp.downstream_manifests("hub")
    assert "DOWNSTREAM_PINS_FILE" in str(exc.value)


# --- failure summary de-duplication -----------------------------------------


def test_failure_summary_dedupes_repeated_manifest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Two unparsable requirements in ONE manifest record two failures at their
    # sites; the summary line names the manifest once.
    text = (
        '[project]\nname = "acme-app"\n'
        'dependencies = [\n  "=== bad one ===",\n  "=== bad two ===",\n  "tai42-contract>=9,<10",\n]\n'
    )
    fake = FakeGitHub({_contents_url("acme-app", "pyproject.toml"): text})
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"]})
    out = capsys.readouterr().out
    assert rc == 1
    summary = next(line for line in out.splitlines() if "downstream-pin sweep failed for" in line)
    assert summary.count("read acme-app/pyproject.toml") == 1


# --- issue-listing failure isolates to its repo -----------------------------


def test_listing_failure_for_one_repo_skips_it_but_reports_others(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub(
        {
            _contents_url("acme-app", "pyproject.toml"): _manifest(">=1.5,<2"),
            _contents_url("beta", "pyproject.toml"): _manifest(">=1.5,<2"),
        },
        list_error_repos={"acme-app"},
    )
    rc = _run_main(monkeypatch, fake, {"acme-app": ["pyproject.toml"], "beta": ["pyproject.toml"]})
    out = capsys.readouterr().out
    assert rc == 1
    assert "failed to list open issues on acme-app" in out
    assert "list acme-app" in out  # de-duplicated summary entry
    # acme-app's titles were skipped: nothing written to its issues endpoint.
    assert all(url != f"{dp.API}/repos/{OWNER}/acme-app/issues" for url, _ in fake.writes("POST"))
    # The healthy repo was still reported.
    assert any(url == f"{dp.API}/repos/{OWNER}/beta/issues" for url, _ in fake.writes("POST"))


def test_release_fired_but_no_manifests_configured_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeGitHub({})
    monkeypatch.setattr(dp, "_request", fake.request)
    monkeypatch.setenv("DOWNSTREAM_PINS_MANIFESTS", "{}")
    rc = dp.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error::" in out
    assert "no downstream manifests are configured" in out
    assert fake.fetches() == []


def test_dispatch_sweep_but_no_manifests_configured_exits_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    # The empty-manifest guard is reached on the dispatch door too: targets resolve
    # from a synthetic core tree, so an empty manifest map must still fail loud there.
    _build_core_tree(tmp_path, _core_versions())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    fake = FakeGitHub({})
    monkeypatch.setattr(dp, "_request", fake.request)
    monkeypatch.setenv("DOWNSTREAM_PINS_MANIFESTS", "{}")
    rc = dp.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "::error::" in out
    assert "no downstream manifests are configured" in out
    assert fake.fetches() == []


def test_no_manifest_vars_configured_exits_one_with_single_voice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Neither manifest var is set: downstream_manifests returns an empty map and
    # main is the single voice for that condition — one ::error:: and a non-zero
    # exit, with no separate ::warning:: from the config reader.
    fake = FakeGitHub({})
    monkeypatch.setattr(dp, "_request", fake.request)
    rc = dp.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "no downstream manifests are configured" in out
    assert "::warning::" not in out
    assert fake.fetches() == []


def test_missing_token_exits_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    with pytest.raises(SystemExit) as exc:
        dp.main()
    assert "token" in str(exc.value).lower()


# --- current_core_versions / workflow_dispatch sweep ------------------------

_CORE_PATHS = {
    "tai42-contract": "packages/contract",
    "tai42-kit": "packages/kit",
    "tai42-skeleton": "packages/skeleton",
    "tai42-cli": "packages/cli",
    "tai42-agents": "packages/agents",
}


def _build_core_tree(
    tmp_path,
    versions: dict[str, str],
    *,
    omit_config: set[str] = frozenset(),
    omit_pyproject: set[str] = frozenset(),
    omit_version: set[str] = frozenset(),
) -> None:
    """Write a synthetic release-please-config.json + member pyproject.toml files under ``tmp_path``."""
    import json

    packages = {_CORE_PATHS[name]: {"package-name": name} for name in _CORE_PATHS if name not in omit_config}
    (tmp_path / "release-please-config.json").write_text(json.dumps({"packages": packages}))
    for name, path in _CORE_PATHS.items():
        if name in omit_pyproject:
            continue
        member = tmp_path / path
        member.mkdir(parents=True, exist_ok=True)
        body = '[project]\nname = "member"\n'
        if name not in omit_version:
            body += f'version = "{versions[name]}"\n'
        (member / "pyproject.toml").write_text(body)


def _core_versions() -> dict[str, str]:
    return {name: f"{i + 1}.0.0" for i, name in enumerate(dp.CORE)}


def test_current_core_versions_reads_every_member(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    versions = _core_versions()
    _build_core_tree(tmp_path, versions)
    monkeypatch.chdir(tmp_path)
    assert dp.current_core_versions() == versions


def test_current_core_versions_missing_member_in_config_is_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    versions = _core_versions()
    _build_core_tree(tmp_path, versions, omit_config={"tai42-agents"})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        dp.current_core_versions()
    assert "tai42-agents" in str(exc.value)


def test_current_core_versions_missing_pyproject_is_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    versions = _core_versions()
    _build_core_tree(tmp_path, versions, omit_pyproject={"tai42-cli"})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        dp.current_core_versions()
    assert "tai42-cli" in str(exc.value)


def test_current_core_versions_missing_project_version_is_error(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    versions = _core_versions()
    _build_core_tree(tmp_path, versions, omit_version={"tai42-kit"})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        dp.current_core_versions()
    assert "version" in str(exc.value)


def test_resolve_targets_dispatch_sweeps_every_core_member(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    versions = _core_versions()
    _build_core_tree(tmp_path, versions)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.delenv("GITHUB_REF_NAME", raising=False)
    assert dp.resolve_targets() == versions
