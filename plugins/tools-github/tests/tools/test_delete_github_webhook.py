"""Tests for the ``delete_github_webhook`` tool: the DELETE on a 204, the returned confirmation,
``hook_id`` validation, non-204 propagation, bad repo shape, and missing token."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from tai42_tools_github.tools.delete_github_webhook import delete_github_webhook


def _call(repo: str = "octo/hello", hook_id: int = 42) -> dict[str, Any]:
    return asyncio.run(delete_github_webhook(repo, hook_id))


@pytest.mark.usefixtures("curl_app")
def test_happy_path_deletes_on_204(github_env: Callable[..., None], stub_server: Any) -> None:
    github_env(api_base=stub_server.base_url)
    stub_server.set_responder(lambda _request: (204, {}, ""))

    result = _call(hook_id=42)
    assert result == {"repo": "octo/hello", "hook_id": 42, "deleted": True}

    request = stub_server.requests[0]
    assert request["method"] == "DELETE"
    assert request["path"] == "/repos/octo/hello/hooks/42"


@pytest.mark.parametrize("hook_id", [0, -1, True, "5", 1.5])
def test_non_positive_int_hook_id_raises(hook_id: Any) -> None:
    with pytest.raises(ValueError, match="hook_id"):
        _call(hook_id=hook_id)


@pytest.mark.usefixtures("curl_app")
def test_non_204_propagates(github_env: Callable[..., None], stub_server: Any) -> None:
    github_env(api_base=stub_server.base_url)
    stub_server.set_responder(lambda _request: (404, {"Content-Type": "application/json"}, '{"message": "Not Found"}'))
    with pytest.raises(ValueError, match="delete failed") as excinfo:
        _call()
    message = str(excinfo.value)
    assert "404" in message
    assert "Not Found" in message


def test_bad_repo_shape_raises() -> None:
    with pytest.raises(ValueError, match="repo"):
        _call("not-a-repo")


@pytest.mark.parametrize("token", [None, ""])
def test_missing_or_empty_token_raises(github_env: Callable[..., None], token: str | None) -> None:
    github_env(token=token)
    with pytest.raises(ValueError, match="TOOLS_GITHUB_TOKEN"):
        _call()


def test_registration(load_registrations: Callable[[str], Any]) -> None:
    app = load_registrations("tai42_tools_github.tools.delete_github_webhook")
    assert set(app.tools.registered) == {"delete_github_webhook"}
