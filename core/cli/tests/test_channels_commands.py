"""``tai channels`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


def test_channels_list_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/channels"
        assert request.headers["x-api-key"] == "test-key"
        return data_response({"channels": ["slack", "telegram"]})

    result = run_cli(monkeypatch, handler, ["channels", "list"])
    assert result.exit_code == 0, result.output
    assert "slack" in result.output
    assert "telegram" in result.output


def test_channels_list_json_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response({"channels": ["telegram"]})

    result = run_cli(monkeypatch, handler, ["channels", "list"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"channels": ["telegram"]}


def test_channels_list_error_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("boom", 500)

    result = run_cli(monkeypatch, handler, ["channels", "list"])
    assert result.exit_code != 0
    assert "boom" in result.output
