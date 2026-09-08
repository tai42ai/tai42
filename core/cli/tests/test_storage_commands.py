"""The ``tai storage`` remote command group, exercised against a fake
``/api/storage*`` server.

A storage ``resource_id`` / ``dir_path`` fills a ``{name:path}`` route parameter — a
multi-segment path by contract — so the CLI sends its ``/`` as a raw separator while
percent-encoding each segment; the server's ``:path`` converter matches the whole
subpath and access control admits the raw separator only on these path-typed doors.
"""

from __future__ import annotations

import httpx
import pytest

from .remote_harness import data_response, run_cli

# A resource id spanning two segments, the second carrying reserved characters (a space and
# a ``#``) that must percent-encode WITHIN the segment while the ``/`` stays a raw separator.
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
