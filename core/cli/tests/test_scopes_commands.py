"""``tai scopes`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


def test_scopes_routes_renders_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/auth/routes"
        return data_response([{"path": "/api/tools", "methods": ["GET"], "mapped": None}])

    result = run_cli(monkeypatch, handler, ["scopes", "routes"])
    assert result.exit_code == 0, result.output
    assert "/api/tools" in result.output


def test_scopes_public_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/auth/public-routes"
        return data_response(["/universal_webhook/events"])

    result = run_cli(monkeypatch, handler, ["scopes", "public-list"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == ["/universal_webhook/events"]


def test_scopes_public_pin_sends_url_and_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/public-routes"
        assert json.loads(request.content) == {"url": "/open", "pattern": r"/open/\d+"}
        return data_response({"url": "/open"})

    result = run_cli(monkeypatch, handler, ["scopes", "public-pin", "/open", "--pattern", r"/open/\d+"])
    assert result.exit_code == 0, result.output


def test_scopes_public_unpin_sends_delete_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/auth/public-routes"
        assert json.loads(request.content) == {"url": "/open"}
        return data_response({"url": "/open"})

    result = run_cli(monkeypatch, handler, ["scopes", "public-unpin", "/open"])
    assert result.exit_code == 0, result.output


def test_scopes_public_unpin_404_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("url is not pinned public: '/open'", 404)

    result = run_cli(monkeypatch, handler, ["scopes", "public-unpin", "/open"])
    assert result.exit_code != 0
    assert "not pinned public" in result.output


def test_scopes_remove_url_sends_delete_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/auth/scopes/urls"
        assert json.loads(request.content) == {"url": "/api/tools"}
        return data_response({"url": "/api/tools"})

    result = run_cli(monkeypatch, handler, ["scopes", "remove-url", "/api/tools"])
    assert result.exit_code == 0, result.output


def test_scopes_list_renders_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/scopes"
        return data_response({"/api/tools": "read"})

    result = run_cli(monkeypatch, handler, ["scopes", "list"])
    assert result.exit_code == 0, result.output
    assert "/api/tools" in result.output


def test_scopes_add_sends_optional_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/auth/scopes"
        assert json.loads(request.content) == {"scope_id": "read", "url": "/api/tools", "pattern": "^/api/t"}
        return data_response({"scope_id": "read"})

    result = run_cli(monkeypatch, handler, ["scopes", "add", "read", "/api/tools", "--pattern", "^/api/t"])
    assert result.exit_code == 0, result.output


def test_scopes_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/auth/scopes/read"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["scopes", "delete", "read"])
    assert result.exit_code == 0, result.output
