"""Tests for the ``create_github_webhook`` tool: the POST body/URL/headers, the returned shape,
argument validation, missing/empty token, non-2xx propagation, refused redirect, and that no raise
or return ever carries the caller's secret or the token."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_github._internal.tools.github_client import GITHUB_ACCEPT, GITHUB_API_VERSION
from tai42_tools_github.tools.create_github_webhook import create_github_webhook

_HOOK = '{"id": 42, "config": {"url": "https://hooks.example/x"}, "events": ["push"], "active": true}'


def _ok(_request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
    return 201, {"Content-Type": "application/json"}, _HOOK


def _call(
    repo: str = "octo/hello",
    url: str = "https://hooks.example/x",
    events: list[str] | None = None,
    secret: str = "s3cr3t",
) -> dict[str, Any]:
    return asyncio.run(create_github_webhook(repo, url, events or ["push"], secret))


@pytest.mark.usefixtures("curl_app")
def test_happy_path_posts_expected_request(github_env: Callable[..., None], stub_server: Any) -> None:
    github_env(token="gh-token", api_base=stub_server.base_url)
    stub_server.set_responder(_ok)

    result = _call(events=["push", "pull_request"], secret="s3cr3t")
    assert result == {"id": 42, "url": "https://hooks.example/x", "events": ["push"], "active": True}

    request = stub_server.requests[0]
    assert request["method"] == "POST"
    assert request["path"] == "/repos/octo/hello/hooks"

    headers = request["headers"]
    assert headers["authorization"] == "Bearer gh-token"
    assert headers["accept"] == GITHUB_ACCEPT
    assert headers["x-github-api-version"] == GITHUB_API_VERSION

    body = json.loads(request["body"])
    assert body == {
        "config": {"url": "https://hooks.example/x", "content_type": "json", "secret": "s3cr3t"},
        "events": ["push", "pull_request"],
    }


@pytest.mark.usefixtures("curl_app")
def test_secret_is_sent_but_never_returned(github_env: Callable[..., None], stub_server: Any) -> None:
    github_env(api_base=stub_server.base_url)
    stub_server.set_responder(_ok)
    result = _call(secret="topsecretvalue")
    # The secret rides the request body but appears in no returned field.
    assert "topsecretvalue" in stub_server.requests[0]["body"]
    assert "topsecretvalue" not in json.dumps(result)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"repo": "no-slash"}, "repo"),
        ({"repo": "a/b/c"}, "repo"),
        ({"repo": "own er/repo"}, "repo"),
        ({"url": ""}, "url"),
        ({"url": "http://insecure.example"}, "url"),
        ({"url": "https://"}, "url"),  # scheme only, no host
        ({"events": []}, "events"),
        ({"secret": ""}, "secret"),
    ],
)
def test_argument_validation_raises(kwargs: dict[str, Any], match: str) -> None:
    base: dict[str, Any] = {
        "repo": "octo/hello",
        "url": "https://hooks.example/x",
        "events": ["push"],
        "secret": "s3cr3t",
    }
    base.update(kwargs)
    with pytest.raises(ValueError, match=match):
        asyncio.run(create_github_webhook(base["repo"], base["url"], base["events"], base["secret"]))


@pytest.mark.usefixtures("curl_app")
def test_non_2xx_propagates_without_leaking_token_or_secret(github_env: Callable[..., None], stub_server: Any) -> None:
    github_env(token="gh-token", api_base=stub_server.base_url)
    stub_server.set_responder(
        lambda _request: (422, {"Content-Type": "application/json"}, '{"message": "Validation Failed"}')
    )
    with pytest.raises(ValueError, match="create failed") as excinfo:
        _call(secret="topsecretvalue")
    message = str(excinfo.value)
    assert "422" in message
    assert "Validation Failed" in message
    assert "topsecretvalue" not in message
    assert "gh-token" not in message


@pytest.mark.usefixtures("curl_app")
def test_redirect_is_refused_and_hides_credentials(
    github_env: Callable[..., None], stub_server: Any, local_server: Any
) -> None:
    github_env(token="gh-token", api_base=stub_server.base_url)
    target = f"{local_server.base_url}/repos/octo/hello/hooks"
    stub_server.set_responder(lambda _request: (302, {"Location": target}, ""))
    with pytest.raises(ValueError, match="HTTP 302") as excinfo:
        _call(secret="topsecretvalue")
    # The redirect target recorded no request: the Authorization header never left the pinned host.
    assert local_server.requests == []
    message = str(excinfo.value)
    assert "gh-token" not in message
    assert "Bearer" not in message
    assert "topsecretvalue" not in message


@pytest.mark.parametrize("token", [None, ""])
def test_missing_or_empty_token_raises(github_env: Callable[..., None], token: str | None) -> None:
    github_env(token=token)
    with pytest.raises(ValueError, match="TOOLS_GITHUB_TOKEN"):
        _call()


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_github.tools.create_github_webhook")
    assert set(app.tools.registered) == {"create_github_webhook"}
