"""``tai system`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_system_kinds_renders_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/system/kinds"
        return data_response(
            [
                {"kind": "monitoring", "state": "default", "plugin": None, "detail": "noop"},
                {"kind": "storage", "state": "off", "plugin": None, "detail": "dead by default"},
            ]
        )

    result = run_cli(monkeypatch, handler, ["system", "kinds"])
    assert result.exit_code == 0, result.output
    assert "monitoring" in result.output
    assert "storage" in result.output


def test_system_kinds_json_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [{"kind": "config", "state": "default", "plugin": None, "detail": "file"}]

    def handler(request: httpx.Request) -> httpx.Response:
        return data_response(rows)

    result = run_cli(monkeypatch, handler, ["system", "kinds"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == rows


def test_system_kinds_renders_the_files_row_with_its_install_line(monkeypatch: pytest.MonkeyPatch) -> None:
    install = (
        "tai42-skeleton 'files' resource loading requires the 'files' optional "
        "dependencies (magika, pandas, langchain-community, pypdf, docx2txt, "
        "openpyxl, tabulate, beautifulsoup4, python-pptx, striprtf, odfpy, "
        "ebooklib). Install them with: pip install 'tai42-skeleton[files]'"
    )
    rows = [{"kind": "files", "state": "off", "plugin": None, "detail": install}]
    result = run_cli(monkeypatch, lambda r: data_response(rows), ["system", "kinds"])
    assert result.exit_code == 0, result.output
    header, _rule, row = result.stdout.strip().splitlines()
    assert header.split() == ["kind", "state", "plugin", "detail"]
    # A null plugin renders as an empty cell, so the row reads kind, state, then the detail.
    assert row.split(maxsplit=2) == ["files", "off", install]
