"""``tai channels`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli

_LISTING = {
    "channels": [
        {"name": "slack", "identity_hint": "The bot's Slack user id."},
        {"name": "web", "identity_hint": None},
    ]
}


def test_channels_list_tabulates_name_and_identity_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/channels"
        assert request.headers["x-api-key"] == "test-key"
        return data_response(_LISTING)

    result = run_cli(monkeypatch, handler, ["channels", "list"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0]
    assert header.split() == ["name", "identity_hint"]
    assert "The bot's Slack user id." in result.output
    assert "web" in result.output


def test_channels_list_json_carries_the_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response(_LISTING)

    result = run_cli(monkeypatch, handler, ["channels", "list"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == _LISTING


def test_channels_list_error_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("boom", 500)

    result = run_cli(monkeypatch, handler, ["channels", "list"])
    assert result.exit_code != 0
    assert "boom" in result.output
