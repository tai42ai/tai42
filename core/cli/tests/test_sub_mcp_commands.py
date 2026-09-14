"""``tai sub-mcp`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_sub_mcp_register(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"slug": "account", "tools": ["convert"]}
        return data_response({"slug": "account", "tools": ["convert"]})

    result = run_cli(monkeypatch, handler, ["sub-mcp", "register", "account", "--tool", "convert"])
    assert result.exit_code == 0, result.output


def test_sub_mcp_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/sub-mcp"
        return data_response({"apps": [{"slug": "account"}]})

    result = run_cli(monkeypatch, handler, ["sub-mcp", "list"])
    assert result.exit_code == 0, result.output
    assert "account" in result.output


def test_sub_mcp_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/sub-mcp/account"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["sub-mcp", "delete", "account"])
    assert result.exit_code == 0, result.output
