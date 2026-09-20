"""``tai connectors`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


def test_connectors_connect_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/connections/start"
        body = json.loads(request.content)
        assert body["provider_id"] == "google"
        assert body["enabled_sub_services"] == ["gmail"]
        return data_response({"flow_id": "f1", "authorize_url": "https://x"})

    result = run_cli(
        monkeypatch, handler, ["connectors", "connect", "google", "--alias", "work", "--sub-service", "gmail"]
    )
    assert result.exit_code == 0, result.output


def test_connectors_get_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("connection not found", 404)

    result = run_cli(monkeypatch, handler, ["connectors", "get", "missing"])
    assert result.exit_code != 0
    assert "connection not found" in result.output


def test_connectors_providers_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/providers"
        return data_response(
            {
                "providers": [{"id": "google", "display_name": "Google", "kind": "oauth", "category": "email"}],
                "categories": [{"id": "email", "display_name": "Email", "sort_order": 1}],
            }
        )

    result = run_cli(monkeypatch, handler, ["connectors", "providers"])
    assert result.exit_code == 0, result.output
    assert "google" in result.output


def test_connectors_connections_uses_items_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/connections"
        return data_response(
            {"items": [{"connection_id": "c1", "provider_id": "google", "alias": "work", "auth_health_state": "ok"}]}
        )

    result = run_cli(monkeypatch, handler, ["connectors", "connections"])
    assert result.exit_code == 0, result.output
    assert "c1" in result.output


def test_connectors_get_happy(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/connections/c1"
        return data_response({"connection_id": "c1", "alias": "work"})

    result = run_cli(monkeypatch, handler, ["connectors", "get", "c1"])
    assert result.exit_code == 0, result.output
    assert "work" in result.output


def test_connectors_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/connectors/connections/c1"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["connectors", "disconnect", "c1"])
    assert result.exit_code == 0, result.output


def test_connectors_reconnect_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/connectors/connections/c1/reconnect"
        assert json.loads(request.content) == {"enabled_sub_services": ["gmail"], "return_url": "/connectors"}
        return data_response({"authorize_url": "https://x"})

    result = run_cli(monkeypatch, handler, ["connectors", "reconnect", "c1", "--sub-service", "gmail"])
    assert result.exit_code == 0, result.output


def test_connectors_sub_services_patches(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/connectors/connections/c1/sub-services"
        assert json.loads(request.content) == {
            "enabled_sub_services": ["gmail", "calendar"],
            "return_url": "/connectors",
        }
        return data_response({"enabled_sub_services": ["gmail", "calendar"]})

    result = run_cli(
        monkeypatch,
        handler,
        ["connectors", "sub-services", "c1", "--sub-service", "gmail", "--sub-service", "calendar"],
    )
    assert result.exit_code == 0, result.output


def _capture_connect(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/connectors/connections/start"
        seen.update(json.loads(request.content))
        return data_response({"authorize_url": "https://x"})

    return handler


def test_connect_reads_config_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config_file = tmp_path / "c.json"
    config_file.write_text('{"api_key":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_connect(seen),
        ["connectors", "connect", "prov", "--alias", "work", "--sub-service", "svc", "--config-file", str(config_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["config_values"] == {"api_key": "secret"}


def test_connect_reads_config_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_connect(seen),
        ["connectors", "connect", "prov", "--alias", "work", "--sub-service", "svc", "--config-file", "-"],
        stdin='{"api_key":"secret"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["config_values"] == {"api_key": "secret"}


def test_connect_rejects_both_config_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config_file = tmp_path / "c.json"
    config_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_connect({}),
        [
            "connectors",
            "connect",
            "prov",
            "--alias",
            "work",
            "--sub-service",
            "svc",
            "--config",
            "{}",
            "--config-file",
            str(config_file),
        ],
    )
    assert result.exit_code != 0
    assert "--config-file" in result.output
