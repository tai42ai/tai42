"""Tests for the shared GitHub client module: repo-shape validation, per-segment path encoding
(observed at the seam), Link ``rel="next"`` parsing, and the list page ceiling.

The path-encoding and ceiling tests replace the ``_http_request`` seam with an async fake so the
exact URL and the loop bound are asserted without the network; the curl transport itself is
exercised by the tool suites against a loopback stub."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_github._internal.tools import github_client
from tai42_tools_github._internal.tools.github_client import (
    _LIST_PAGE_CEILING,
    _hooks_url,
    _next_page_url,
    _split_repo,
    create_webhook,
    delete_webhook,
    list_webhooks,
)

# --- repo shape + path encoding -----------------------------------------------------------


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("octo/hello", ("octo", "hello")),
        ("octo-org/repo.name", ("octo-org", "repo.name")),
    ],
)
def test_split_repo_accepts_owner_name(repo: str, expected: tuple[str, str]) -> None:
    assert _split_repo(repo) == expected


@pytest.mark.parametrize("repo", ["", "no-slash", "a/b/c", "/name", "owner/", "own er/repo", "owner/re po", 123])
def test_split_repo_rejects_bad_shape(repo: Any) -> None:
    with pytest.raises(ValueError, match="repo"):
        _split_repo(repo)


def test_hooks_url_percent_encodes_each_segment(github_env: Callable[..., None]) -> None:
    github_env(api_base="https://api.github.test")
    # '#' and '?' pass the shape check (no slash, no whitespace) and must be encoded per segment so
    # neither can escape into a different path.
    assert _hooks_url("own#er", "re?po") == "https://api.github.test/repos/own%23er/re%3Fpo/hooks"


def test_delete_url_uses_encoded_segments(github_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch) -> None:
    github_env(api_base="https://api.github.test")
    captured: dict[str, str] = {}

    async def fake(method: str, url: str, **_kwargs: Any) -> tuple[int, dict[str, str], str]:
        captured["method"] = method
        captured["url"] = url
        return 204, {}, ""

    monkeypatch.setattr(github_client, "_http_request", fake)
    asyncio.run(delete_webhook("own#er/re?po", 7))
    assert captured["method"] == "DELETE"
    assert captured["url"] == "https://api.github.test/repos/own%23er/re%3Fpo/hooks/7"


def test_create_url_uses_encoded_segments(github_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch) -> None:
    github_env(api_base="https://api.github.test")
    captured: dict[str, str] = {}

    async def fake(method: str, url: str, **_kwargs: Any) -> tuple[int, dict[str, str], str]:
        captured["url"] = url
        return 201, {}, '{"id": 1, "config": {"url": "u"}, "events": [], "active": true}'

    monkeypatch.setattr(github_client, "_http_request", fake)
    asyncio.run(create_webhook("own#er/re?po", "https://h", ["push"], "s"))
    assert captured["url"] == "https://api.github.test/repos/own%23er/re%3Fpo/hooks"


# --- Link header parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, None),
        ("", None),
        ('<https://x?page=2>; rel="last"', None),
        ('<https://x?page=2>; rel="next"', "https://x?page=2"),
        ('<https://x?page=3>; rel="last", <https://x?page=2>; rel="next"', "https://x?page=2"),
    ],
)
def test_next_page_url(header: str | None, expected: str | None) -> None:
    assert _next_page_url(header) == expected


# --- the list page ceiling ----------------------------------------------------------------


def test_list_page_ceiling_raises(github_env: Callable[..., None], monkeypatch: pytest.MonkeyPatch) -> None:
    github_env(api_base="https://api.github.test")
    calls = {"n": 0}

    async def fake(_method: str, _url: str, **_kwargs: Any) -> tuple[int, dict[str, str], str]:
        calls["n"] += 1
        # Always name a next page: a self-referential Link header must hit the ceiling, not loop.
        return 200, {"link": f'<https://api.github.test/x?page={calls["n"] + 1}>; rel="next"'}, "[]"

    monkeypatch.setattr(github_client, "_http_request", fake)
    with pytest.raises(ValueError, match="page ceiling"):
        asyncio.run(list_webhooks("octo/hello"))
    assert calls["n"] == _LIST_PAGE_CEILING
