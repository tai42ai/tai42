"""``tai traces`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_traces_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/observability/runs"
        return data_response({"items": [{"traceId": "t1", "status": "ok"}], "page": 1, "nextPage": None})

    result = run_cli(monkeypatch, handler, ["traces", "list"])
    assert result.exit_code == 0, result.output
    assert "t1" in result.output


def test_traces_export_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/observability/runs/export"
        assert request.url.params.get("format") == "csv"
        return httpx.Response(200, text="traceId,status\nt1,ok\n", headers={"content-type": "text/csv"})

    result = run_cli(monkeypatch, handler, ["traces", "list", "--export", "--format", "csv"])
    assert result.exit_code == 0, result.output
    assert "traceId,status" in result.output


def test_traces_list_passes_all_filters(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/observability/runs"
        params = request.url.params
        assert params.get("from") == "30d"
        assert params.get("to") == "now"
        assert params.get("status") == "error"
        assert params.get("user") == "u_42"
        assert params.get("session") == "sess_7"
        assert params.get("version") == "preset-v3"
        assert params.get("sort") == "cost"
        assert params.get("dir") == "desc"
        assert params.get("page") == "2"
        assert params.get("pageSize") == "50"
        return data_response({"items": [{"traceId": "t9", "status": "error"}], "page": 2})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "traces",
            "list",
            "--from",
            "30d",
            "--to",
            "now",
            "--status",
            "error",
            "--user",
            "u_42",
            "--session",
            "sess_7",
            "--version",
            "preset-v3",
            "--sort",
            "cost",
            "--dir",
            "desc",
            "--page",
            "2",
            "--page-size",
            "50",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "t9" in result.output


def test_traces_get_full_trace(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/observability/runs/trace_abc/trace"
        return data_response({"traceId": "trace_abc", "spans": []})

    result = run_cli(monkeypatch, handler, ["traces", "get", "trace_abc"])
    assert result.exit_code == 0, result.output
    assert "trace_abc" in result.output


def test_traces_get_export_downloads_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/observability/runs/trace_abc/trace/export"
        return httpx.Response(200, json={"traceId": "trace_abc"}, headers={"content-type": "application/json"})

    result = run_cli(monkeypatch, handler, ["traces", "get", "trace_abc", "--export"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"traceId": "trace_abc"}
