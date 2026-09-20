"""The ``tai storage`` remote command group, exercised against a fake
``/api/storage*`` server.

A storage ``resource_id`` / ``dir_path`` fills a ``{name:path}`` route parameter — a
multi-segment path by contract — so the CLI sends its ``/`` as a raw separator while
percent-encoding each segment; the server's ``:path`` converter matches the whole
subpath and access control admits the raw separator only on these path-typed doors.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from .remote_harness import data_response, run_cli

_SLASH_ID = "images/logo v2#final.png"


_ENCODED_ID = "images/logo%20v2%23final.png"


def test_stat_sends_the_id_as_a_raw_slash_subpath(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # The ``/`` stays a raw separator (the ``:path`` converter matches the whole
        # subpath); each segment's reserved characters are percent-encoded.
        assert request.url.raw_path.decode() == f"/api/storage/resources/{_ENCODED_ID}/stat"
        assert request.url.path == f"/api/storage/resources/{_SLASH_ID}/stat"
        return data_response({"content_type": "image/png"})

    result = run_cli(monkeypatch, handler, ["storage", "stat", _SLASH_ID])
    assert result.exit_code == 0, result.output


def test_download_sends_the_id_as_a_raw_slash_subpath(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.decode() == f"/api/storage/resources/{_ENCODED_ID}/content"
        return httpx.Response(200, content=b"bytes")

    result = run_cli(monkeypatch, handler, ["storage", "download", _SLASH_ID])
    assert result.exit_code == 0, result.output


def test_delete_sends_the_id_as_a_raw_slash_subpath(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.raw_path.decode() == f"/api/storage/resources/{_ENCODED_ID}"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["storage", "delete", _SLASH_ID])
    assert result.exit_code == 0, result.output


def test_delete_dir_sends_the_path_as_a_raw_slash_subpath(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.raw_path.decode() == "/api/storage/dirs/notes/2026%20q3"
        return data_response({"deleted": 3})

    result = run_cli(monkeypatch, handler, ["storage", "delete-dir", "notes/2026 q3"])
    assert result.exit_code == 0, result.output


def test_storage_info_list_stat(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(monkeypatch, lambda r: data_response({"provider": "local"}), ["storage", "info"]).exit_code == 0

    def list_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/resources"
        return data_response({"resources": ["a.txt"]})

    assert run_cli(monkeypatch, list_handler, ["storage", "list"]).exit_code == 0

    def stat_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/resources/a.txt/stat"
        return data_response({"content_type": "text/plain"})

    assert run_cli(monkeypatch, stat_handler, ["storage", "stat", "a.txt"]).exit_code == 0


def test_storage_download_streams_raw_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/resources/a.txt/content"
        return httpx.Response(200, content=b"hello bytes")

    result = run_cli(monkeypatch, handler, ["storage", "download", "a.txt"], json_output=False)
    assert result.exit_code == 0, result.output
    assert "hello bytes" in result.output


def test_storage_upload_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body == {"id": "a.txt", "content_text": "hi"}
        return data_response({"id": "a.txt"})

    assert run_cli(monkeypatch, handler, ["storage", "upload", "a.txt", "--text", "hi"]).exit_code == 0


def test_storage_upload_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"id": "a.bin", "content_base64": "AAAA"}
        return data_response({"id": "a.bin"})

    assert run_cli(monkeypatch, handler, ["storage", "upload", "a.bin", "--base64", "AAAA"]).exit_code == 0


def test_storage_upload_rejects_both_or_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    both = run_cli(monkeypatch, lambda r: data_response({}), ["storage", "upload", "a", "--text", "x", "--base64", "y"])
    assert both.exit_code != 0
    neither = run_cli(monkeypatch, lambda r: data_response({}), ["storage", "upload", "a"])
    assert neither.exit_code != 0


def test_storage_delete_and_delete_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    def del_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/storage/resources/a.txt"
        return data_response({"deleted": True})

    assert run_cli(monkeypatch, del_handler, ["storage", "delete", "a.txt"]).exit_code == 0

    def dir_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/dirs/notes"
        return data_response({"deleted": True})

    assert run_cli(monkeypatch, dir_handler, ["storage", "delete-dir", "notes"]).exit_code == 0


def _capture_upload(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/resources"
        seen.update(json.loads(request.content))
        return data_response({"id": "r"})

    return handler


def test_storage_upload_reads_file_bytes(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    content = b"\x00secret-bytes\xff"
    src = tmp_path / "blob.bin"
    src.write_bytes(content)
    seen: dict = {}
    result = run_cli(monkeypatch, _capture_upload(seen), ["storage", "upload", "r", "--file", str(src)])
    assert result.exit_code == 0, result.output
    assert base64.b64decode(seen["content_base64"]) == content


def test_storage_upload_reads_bytes_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    content = b"\x00secret-bytes\xff"
    seen: dict = {}
    result = run_cli(monkeypatch, _capture_upload(seen), ["storage", "upload", "r", "--file", "-"], stdin=content)
    assert result.exit_code == 0, result.output
    assert base64.b64decode(seen["content_base64"]) == content


def test_storage_upload_rejects_text_and_file_together(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    src = tmp_path / "blob.bin"
    src.write_bytes(b"x")
    result = run_cli(
        monkeypatch,
        _capture_upload({}),
        ["storage", "upload", "r", "--text", "hi", "--file", str(src)],
    )
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_storage_upload_rejects_no_source(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _capture_upload({}), ["storage", "upload", "r"])
    assert result.exit_code != 0
    assert "exactly one" in result.output


def test_storage_upload_missing_file_raises(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    result = run_cli(
        monkeypatch,
        _capture_upload({}),
        ["storage", "upload", "r", "--file", str(tmp_path / "nope.bin")],
    )
    assert result.exit_code != 0
    assert "--file" in result.output
