"""Tests for the ``list_github_webhooks`` tool: the GET request, the returned per-hook shape,
Link-header pagination to exhaustion, non-2xx propagation, bad repo shape, and missing token."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_github.tools.list_github_webhooks import list_github_webhooks


def _hook(hook_id: int, url: str = "https://hooks.example/x", active: bool = True) -> str:
    active_json = "true" if active else "false"
    return f'{{"id": {hook_id}, "config": {{"url": "{url}"}}, "events": ["push"], "active": {active_json}}}'


def _call(repo: str = "octo/hello") -> list[dict[str, Any]]:
    return asyncio.run(list_github_webhooks(repo))


@pytest.mark.usefixtures("curl_app")
def test_happy_path_single_page(github_env: Callable[..., None], stub_server: Any) -> None:
    github_env(api_base=stub_server.base_url)
    stub_server.set_responder(
        lambda _request: (200, {"Content-Type": "application/json"}, f"[{_hook(1)}, {_hook(2, active=False)}]")
    )

    result = _call()
    assert result == [
        {"id": 1, "url": "https://hooks.example/x", "events": ["push"], "active": True},
        {"id": 2, "url": "https://hooks.example/x", "events": ["push"], "active": False},
    ]
    request = stub_server.requests[0]
    assert request["method"] == "GET"
    assert request["path"] == "/repos/octo/hello/hooks"


@pytest.mark.usefixtures("curl_app")
def test_follows_link_next_to_exhaustion(github_env: Callable[..., None], stub_server: Any) -> None:
    github_env(api_base=stub_server.base_url)
    next_url = f"{stub_server.base_url}/repos/octo/hello/hooks?page=2"

    def responder(request: dict[str, Any]) -> tuple[int, dict[str, str], str]:
        if request["query"].get("page") == ["2"]:
            return 200, {"Content-Type": "application/json"}, f"[{_hook(2)}]"
        return 200, {"Link": f'<{next_url}>; rel="next", <...?page=9>; rel="last"'}, f"[{_hook(1)}]"

    stub_server.set_responder(responder)

    result = _call()
    assert [hook["id"] for hook in result] == [1, 2]
    assert len(stub_server.requests) == 2
    assert stub_server.requests[1]["query"]["page"] == ["2"]


@pytest.mark.usefixtures("curl_app")
def test_last_only_link_stops(github_env: Callable[..., None], stub_server: Any) -> None:
    github_env(api_base=stub_server.base_url)
    stub_server.set_responder(lambda _request: (200, {"Link": '<https://x?page=1>; rel="last"'}, f"[{_hook(1)}]"))
    result = _call()
    assert [hook["id"] for hook in result] == [1]
    assert len(stub_server.requests) == 1


@pytest.mark.usefixtures("curl_app")
def test_non_2xx_propagates(github_env: Callable[..., None], stub_server: Any) -> None:
    github_env(api_base=stub_server.base_url)
    stub_server.set_responder(lambda _request: (500, {}, "server error"))
    with pytest.raises(ValueError, match="list failed") as excinfo:
        _call()
    assert "500" in str(excinfo.value)


def test_bad_repo_shape_raises() -> None:
    with pytest.raises(ValueError, match="repo"):
        _call("not-a-repo")


@pytest.mark.parametrize("token", [None, ""])
def test_missing_or_empty_token_raises(github_env: Callable[..., None], token: str | None) -> None:
    github_env(token=token)
    with pytest.raises(ValueError, match="TOOLS_GITHUB_TOKEN"):
        _call()


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_github.tools.list_github_webhooks")
    assert set(app.tools.registered) == {"list_github_webhooks"}
