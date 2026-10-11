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


_ROW = {
    "name": "nightly",
    "tool": "report",
    "cadence": {"kind": "interval", "every": 60.0, "relative": False},
    "enabled": True,
    "next_run_at_ts": None,
    "next_run_at_iso": "2026-07-08T09:00:00+00:00",
    "error": None,
}


def test_schedules_list_renders_the_typed_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/schedules"
        return data_response([_ROW])

    result = run_cli(monkeypatch, handler, ["schedules", "list"])
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert header[:4] == ["name", "tool", "cadence", "enabled"]
    assert "nightly" in result.output
    assert "report" in result.output
    assert "2026-07-08T09:00:00+00:00" in result.output


def test_schedules_server_datetime(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/schedules/server-datetime"
        return data_response({"now": "2026-07-08T00:00:00Z"})

    result = run_cli(monkeypatch, handler, ["schedules", "server-datetime"])
    assert result.exit_code == 0, result.output
    assert "2026-07-08" in result.output


def _capture(bodies: list[dict]):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/schedules"
        bodies.append(json.loads(request.content))
        return data_response({"scheduled": "report"})

    return handler


def test_schedules_add_sends_the_base_tool_and_a_cron_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []
    result = run_cli(
        monkeypatch,
        _capture(bodies),
        ["schedules", "add", "report", "--tool-kw", "n=5", "--cron", "0 9 * * *", "--schedule-name", "morning"],
    )
    assert result.exit_code == 0, result.output
    assert bodies == [
        {
            "tool_name": "report",
            "tool_kwargs": {"n": 5},
            "cadence": {
                "kind": "crontab",
                "minute": "0",
                "hour": "9",
                "day_of_month": "*",
                "month_of_year": "*",
                "day_of_week": "*",
            },
            "schedule_name": "morning",
        }
    ]


def test_schedules_add_accepts_a_six_field_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []
    result = run_cli(monkeypatch, _capture(bodies), ["schedules", "add", "report", "--cron", "30 0 9 * * 1"])
    assert result.exit_code == 0, result.output
    assert bodies[0]["cadence"]["minute"] == "0"
    assert bodies[0]["cadence"]["day_of_week"] == "1"


def test_schedules_add_sends_an_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []
    result = run_cli(monkeypatch, _capture(bodies), ["schedules", "add", "report", "--every", "90"])
    assert result.exit_code == 0, result.output
    assert bodies[0]["cadence"] == {"kind": "interval", "every": 90.0, "relative": False}


def test_schedules_add_sends_a_json_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []
    result = run_cli(
        monkeypatch,
        _capture(bodies),
        ["schedules", "add", "report", "--cadence", '{"kind": "interval", "every": 5, "relative": true}'],
    )
    assert result.exit_code == 0, result.output
    assert bodies[0]["cadence"] == {"kind": "interval", "every": 5.0, "relative": True}


def test_schedules_add_without_a_cadence_runs_once(monkeypatch: pytest.MonkeyPatch) -> None:
    bodies: list[dict] = []
    result = run_cli(monkeypatch, _capture(bodies), ["schedules", "add", "report"])
    assert result.exit_code == 0, result.output
    assert bodies == [{"tool_name": "report", "tool_kwargs": {}}]


@pytest.mark.parametrize(
    "options",
    [
        ["--every", "5", "--cron", "* * * * *"],
        ["--cron", "* * * * *", "--cadence", '{"kind": "interval", "every": 5}'],
        ["--every", "5", "--cadence", '{"kind": "interval", "every": 5}'],
    ],
)
def test_schedules_add_cadence_options_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, options: list[str]
) -> None:
    bodies: list[dict] = []
    result = run_cli(monkeypatch, _capture(bodies), ["schedules", "add", "report", *options])
    assert result.exit_code != 0
    assert "at most one cadence option" in result.output
    assert bodies == []


@pytest.mark.parametrize(
    "options",
    [
        ["--cron", "* * * *"],
        ["--every", "0"],
        ["--schedule-name", "orphan"],
        ["--cadence", '{"kind": "solar"}'],
    ],
)
def test_schedules_add_an_invalid_body_is_a_bad_parameter(monkeypatch: pytest.MonkeyPatch, options: list[str]) -> None:
    bodies: list[dict] = []
    result = run_cli(monkeypatch, _capture(bodies), ["schedules", "add", "report", *options])
    assert result.exit_code != 0
    assert "Invalid value" in result.output
    assert bodies == []


_BINDING = {"states": [{"state": "status", "subject_expr": {"content": ".x"}, "templates": ["t1"]}]}


def test_schedules_add_sends_the_door_contract_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["execution_key"] == "svc"
        assert body["start_expr"] == {"content": ".payload"}
        assert body["cancel_expr"] == {"content": "$parked[].id"}
        assert body["resume_expr"] == {"content": "$parked[0].id"}
        assert body["extras_expr"] == {"content": "{warm: .seed}"}
        assert body["state_binding"] == _BINDING
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
            json.dumps(_BINDING),
        ],
    )
    assert result.exit_code == 0, result.output


def test_schedules_add_sends_the_subject_door_field(monkeypatch: pytest.MonkeyPatch) -> None:
    subject = {"target_kind": "agent", "target_name": "a-42", "kind": "job", "key": "j1"}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["subject"] == subject
        return data_response({"name": "report"})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "schedules",
            "add",
            "report",
            "--subject",
            '{"target_kind": "agent", "target_name": "a-42", "kind": "job", "key": "j1"}',
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
