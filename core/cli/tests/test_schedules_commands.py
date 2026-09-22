"""``tai schedules`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


def test_schedules_list_501_surfaces(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("no installed backend exposes scheduling tools", 501)

    result = run_cli(monkeypatch, handler, ["schedules", "list"])
    assert result.exit_code != 0
    assert "scheduling tools" in result.output


def test_schedules_list_happy_renders(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/schedules"
        return data_response({"schedules": [{"name": "report"}]})

    result = run_cli(monkeypatch, handler, ["schedules", "list"])
    assert result.exit_code == 0, result.output
    assert "report" in result.output


def test_schedules_server_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/schedules/server-datetime"
        return data_response({"now": "2026-07-08T00:00:00Z"})

    result = run_cli(monkeypatch, handler, ["schedules", "server-datetime"])
    assert result.exit_code == 0, result.output
    assert "2026-07-08" in result.output


def test_schedules_add_merges_tool_and_schedule_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/schedules"
        body = json.loads(request.content)
        assert body["tool_name"] == "report"
        assert body["tool_kwargs"] == {"n": 5}
        assert body["schedule_kwargs"] == {"cron": "0 9 * * *"}
        return data_response({"name": "report"})

    result = run_cli(
        monkeypatch,
        handler,
        ["schedules", "add", "report", "--tool-kw", "n=5", "--schedule-kw", "cron=0 9 * * *"],
    )
    assert result.exit_code == 0, result.output


def test_schedules_add_sends_the_door_contract_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["execution_key"] == "svc"
        assert body["start_expr"] == {"content": ".payload"}
        assert body["cancel_expr"] == {"content": "$parked[].id"}
        assert body["resume_expr"] == {"content": "$parked[0].id"}
        assert body["extras_expr"] == {"content": "{warm: .seed}"}
        assert body["state_binding"] == {"states": []}
        return data_response({"name": "report"})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "schedules",
            "add",
            "report",
            "--execution-key",
            "svc",
            "--start-expr",
            ".payload",
            "--cancel-expr",
            "$parked[].id",
            "--resume-expr",
            "$parked[0].id",
            "--extras-expr",
            "{warm: .seed}",
            "--state-binding",
            '{"states": []}',
        ],
    )
    assert result.exit_code == 0, result.output


def test_schedules_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/schedules/report"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["schedules", "delete", "report"])
    assert result.exit_code == 0, result.output


def _capture_schedule_add(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/schedules"
        seen.update(json.loads(request.content))
        return data_response({"name": "s1"})

    return handler


def test_schedules_add_reads_tool_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "tk.json"
    kwargs_file.write_text('{"token":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch, _capture_schedule_add(seen), ["schedules", "add", "t", "--tool-kwargs-file", str(kwargs_file)]
    )
    assert result.exit_code == 0, result.output
    assert seen["tool_kwargs"] == {"token": "secret"}


def test_schedules_add_reads_tool_kwargs_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_schedule_add(seen),
        ["schedules", "add", "t", "--tool-kwargs-file", "-"],
        stdin='{"token":"secret"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["tool_kwargs"] == {"token": "secret"}


def test_schedules_add_tool_kw_overrides_tool_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "tk.json"
    kwargs_file.write_text('{"a":1,"token":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_schedule_add(seen),
        ["schedules", "add", "t", "--tool-kwargs-file", str(kwargs_file), "--tool-kw", "a=2"],
    )
    assert result.exit_code == 0, result.output
    assert seen["tool_kwargs"] == {"a": 2, "token": "secret"}


def test_schedules_add_rejects_both_tool_kwargs_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "tk.json"
    kwargs_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_schedule_add({}),
        ["schedules", "add", "t", "--tool-kwargs", "{}", "--tool-kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code != 0
    assert "--tool-kwargs-file" in result.output
