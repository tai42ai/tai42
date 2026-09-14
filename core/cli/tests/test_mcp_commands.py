"""``tai mcp`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_mcp_set_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps([{"title": "srv"}]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-config"
        assert json.loads(request.content) == {"mcp": [{"title": "srv"}]}
        return data_response({"reloaded": True})

    result = run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(config)])
    assert result.exit_code == 0, result.output


def test_mcp_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status"
        return data_response({"bindings": []})

    result = run_cli(monkeypatch, handler, ["mcp", "status"])
    assert result.exit_code == 0, result.output


def test_mcp_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-config/schema"
        return data_response({"type": "object"})

    result = run_cli(monkeypatch, handler, ["mcp", "schema"])
    assert result.exit_code == 0, result.output


def test_mcp_set_from_object_with_mcp_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcp": [{"title": "srv"}]}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"mcp": [{"title": "srv"}]}
        return data_response({"reloaded": True})

    result = run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(config)])
    assert result.exit_code == 0, result.output


def test_mcp_set_rejects_malformed_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text("{not json", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(config)])
    assert result.exit_code != 0
    assert "valid JSON" in result.output


def test_mcp_set_rejects_wrong_shape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"other": 1}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(config)])
    assert result.exit_code != 0
    assert "JSON list" in result.output


def test_mcp_reload_by_title(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/mcp-status/my-server/reload"
        return data_response({"reloaded": True})

    result = run_cli(monkeypatch, handler, ["mcp", "reload", "my-server"])
    assert result.exit_code == 0, result.output


def test_mcp_status_schema_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(monkeypatch, lambda r: data_response({"servers": []}), ["mcp", "status"]).exit_code == 0
    assert run_cli(monkeypatch, lambda r: data_response({"type": "object"}), ["mcp", "schema"]).exit_code == 0

    def failed_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status/failed"
        assert request.url.params["targets"] == "serve-1"
        return data_response({"failed": []})

    assert run_cli(monkeypatch, failed_handler, ["mcp", "failed", "--target", "serve-1"]).exit_code == 0


def test_mcp_set_from_object_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcp": [{"title": "srv"}]}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/mcp-config"
        assert json.loads(request.content) == {"mcp": [{"title": "srv"}]}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(cfg)]).exit_code == 0


def test_mcp_set_accepts_bare_array(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps([{"title": "srv"}]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"mcp": [{"title": "srv"}]}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(cfg)]).exit_code == 0


def test_mcp_set_rejects_bad_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{not json", encoding="utf-8")
    assert run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "set", "--file", str(cfg)]).exit_code != 0


def test_mcp_set_rejects_wrong_shape_nonzero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    assert run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "set", "--file", str(cfg)]).exit_code != 0


def test_mcp_add_single_object(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps({"title": "srv"}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/mcp-config/entries"
        assert json.loads(request.content) == {"entries": [{"title": "srv"}], "replace": False}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "add", "--file", str(cfg)]).exit_code == 0


def test_mcp_add_bare_array_keeps_order(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps([{"title": "a"}, {"title": "b"}]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"entries": [{"title": "a"}, {"title": "b"}], "replace": False}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "add", "--file", str(cfg)]).exit_code == 0


def test_mcp_add_entries_object_with_replace(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps({"entries": [{"title": "srv"}]}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"entries": [{"title": "srv"}], "replace": True}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "add", "--file", str(cfg), "--replace"]).exit_code == 0


def test_mcp_add_rejects_bad_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text("{not json", encoding="utf-8")
    assert run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "add", "--file", str(cfg)]).exit_code != 0


def test_mcp_add_rejects_wrong_shape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps(42), encoding="utf-8")
    result = run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "add", "--file", str(cfg)])
    assert result.exit_code != 0
    assert "entries" in result.output


def test_mcp_add_rejects_non_list_entries(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # An "entries" key whose value is not a list is a malformed wrapper, refused
    # locally by name — never forwarded as a non-list body.
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps({"entries": "srv"}), encoding="utf-8")
    result = run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "add", "--file", str(cfg)])
    assert result.exit_code != 0
    assert "entries" in result.output


def test_mcp_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/mcp-config/entries/srv"
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "remove", "srv"]).exit_code == 0


def test_mcp_reload_reload_failed_deregister(monkeypatch: pytest.MonkeyPatch) -> None:
    def reload_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status/srv/reload"
        assert json.loads(request.content) == {"targets": ["serve-1"]}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, reload_handler, ["mcp", "reload", "srv", "--target", "serve-1"]).exit_code == 0

    def reload_failed_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status/reload-failed"
        assert json.loads(request.content) == {"targets": None}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, reload_failed_handler, ["mcp", "reload-failed"]).exit_code == 0

    def deregister_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status/srv/deregister"
        return data_response({"ok": True})

    assert run_cli(monkeypatch, deregister_handler, ["mcp", "deregister", "srv"]).exit_code == 0
