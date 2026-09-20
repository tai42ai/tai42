"""``tai backup`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


def test_backup_export_streams_raw_document(monkeypatch: pytest.MonkeyPatch) -> None:
    document = {"version": 1, "created_at": "t", "sections": {"access_control": {}}, "errors": {}}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/backup/export"
        assert json.loads(request.content) == {"sections": ["access_control"]}
        # A download route: the body is the RAW document, not a {"data": ...} envelope.
        return httpx.Response(200, json=document, headers={"content-disposition": 'attachment; filename="b.json"'})

    result = run_cli(monkeypatch, handler, ["backup", "export", "--section", "access_control"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == document


def test_backup_export_error_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("unknown section(s): bogus", 400)

    result = run_cli(monkeypatch, handler, ["backup", "export", "--section", "bogus"])
    assert result.exit_code != 0
    assert "unknown section" in result.output


def test_backup_sections_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/backup/sections"
        return data_response([{"name": "access_control", "secret": False}])

    result = run_cli(monkeypatch, handler, ["backup", "sections"])
    assert result.exit_code == 0, result.output
    assert "access_control" in result.output


def test_backup_import_posts_document(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    document = {"version": 1, "sections": {"access_control": {}}}
    backup_file = tmp_path / "backup.json"
    backup_file.write_text(json.dumps(document), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/backup/import"
        assert json.loads(request.content) == {"document": document, "sections": ["access_control"]}
        return data_response({"imported": ["access_control"]})

    result = run_cli(monkeypatch, handler, ["backup", "import", str(backup_file), "--section", "access_control"])
    assert result.exit_code == 0, result.output


def test_backup_import_rejects_malformed_json_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    backup_file = tmp_path / "backup.json"
    backup_file.write_text("{not json", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["backup", "import", str(backup_file), "--section", "access_control"])
    assert result.exit_code != 0
    assert "valid JSON" in result.output
