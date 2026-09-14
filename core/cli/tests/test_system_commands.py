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
