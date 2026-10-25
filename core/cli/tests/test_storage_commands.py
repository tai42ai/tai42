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

from .remote_harness import data_response, error_response, run_cli, visible

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


# -- the stored entry --------------------------------------------------------------

_LOCKED = (
    "resource 'docs/locked.pdf' cannot be loaded: 'docs/locked.pdf' could not be extracted as pdf: "
    "FileNotDecryptedError: File has not been decrypted"
)
FILES_INSTALL_LINE = (
    "tai42-skeleton 'files' resource loading requires the 'files' optional "
    "dependencies (magika, pandas, langchain-community, pypdf, docx2txt, "
    "openpyxl, tabulate, beautifulsoup4, python-pptx, striprtf, odfpy, "
    "ebooklib). Install them with: pip install 'tai42-skeleton[files]'"
)


def _non_canonical(key: str) -> str:
    return f"non-canonical store key {key!r}: a key is '/'-separated names with no empty, '.' or '..' segment"


def test_read_prints_a_text_entry_with_no_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = {"kind": "text", "id": "notes/todo.txt", "content_type": "text/plain", "size": 9, "content": "buy milk"}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.raw_path.decode() == "/api/storage/entries/notes/todo.txt"
        return data_response(entry)

    result = run_cli(monkeypatch, handler, ["storage", "read", "notes/todo.txt"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == entry
    assert "schema" not in result.stdout


def test_read_prints_a_binary_entry_as_its_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = {"kind": "binary", "id": "img/a.png", "content_type": "image/png", "size": 120}
    result = run_cli(monkeypatch, lambda r: data_response(entry), ["storage", "read", "img/a.png"])
    assert result.exit_code == 0, result.output
    assert "binary" in result.stdout
    assert "image/png" in result.stdout


def test_read_prints_a_documents_kind_and_extracted_text(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = {
        "kind": "document",
        "id": "docs/notes.pdf",
        "content_type": "application/pdf",
        "size": 600,
        "content": "Quarterly notes",
    }
    result = run_cli(monkeypatch, lambda r: data_response(entry), ["storage", "read", "docs/notes.pdf"])
    assert result.exit_code == 0, result.output
    assert "document" in result.stdout
    assert "Quarterly notes" in result.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["storage", "read", "docs/locked.pdf"], _LOCKED),
        (["storage", "read", "a//b"], _non_canonical("a//b")),
    ],
)
def test_read_prints_the_servers_400_and_exits_1(monkeypatch: pytest.MonkeyPatch, args, message) -> None:
    result = run_cli(monkeypatch, lambda r: error_response(message, 400), args)
    assert result.exit_code == 1
    assert result.stderr.strip() == f"Error (HTTP 400): {message}"


def test_read_without_the_files_extra_prints_the_install_line(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(501, json={"error": FILES_INSTALL_LINE, "code": "files-not-configured"})

    result = run_cli(monkeypatch, handler, ["storage", "read", "notes/todo.txt"])
    assert result.exit_code == 1
    assert result.stderr.strip() == f"Error (HTTP 501): {FILES_INSTALL_LINE}"


# -- canonical keys on the path doors ----------------------------------------------

_PATH_COMMANDS = {
    "stat": "/api/storage/resources/{key}/stat",
    "download": "/api/storage/resources/{key}/content",
    "delete": "/api/storage/resources/{key}",
    "delete-dir": "/api/storage/dirs/{key}",
    "read": "/api/storage/entries/{key}",
}


@pytest.mark.parametrize("command", list(_PATH_COMMANDS))
@pytest.mark.parametrize("key", ["./x", "x/.", "../x"])
def test_a_dot_segment_key_is_refused_before_any_request(monkeypatch: pytest.MonkeyPatch, command, key) -> None:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return data_response({})

    result = run_cli(monkeypatch, handler, ["storage", command, key])
    assert result.exit_code == 2
    assert _non_canonical(key) in visible(result.stderr)
    assert sent == []


@pytest.mark.parametrize("command", list(_PATH_COMMANDS))
@pytest.mark.parametrize("key", ["a//b", "x/"])
def test_an_empty_segment_key_is_sent_as_typed_and_the_servers_400_printed(
    monkeypatch: pytest.MonkeyPatch, command, key
) -> None:
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request.url.raw_path.decode())
        return error_response(_non_canonical(key), 400)

    result = run_cli(monkeypatch, handler, ["storage", command, key])
    assert sent == [_PATH_COMMANDS[command].format(key=key)]
    assert result.exit_code == 1
    assert result.stderr.strip() == f"Error (HTTP 400): {_non_canonical(key)}"


def test_upload_of_a_non_canonical_id_prints_the_servers_400(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["id"] == "a//b"
        return error_response(_non_canonical("a//b"), 400)

    result = run_cli(monkeypatch, handler, ["storage", "upload", "a//b", "--text", "t"])
    assert result.exit_code == 1
    assert result.stderr.strip() == f"Error (HTTP 400): {_non_canonical('a//b')}"
