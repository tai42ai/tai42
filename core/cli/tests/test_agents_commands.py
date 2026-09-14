"""``tai agents`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_agents_list_renders_items(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents"
        return data_response({"items": [{"name": "r", "tool_name": "r_run", "spec_runnable": True}], "total": 1})

    result = run_cli(monkeypatch, handler, ["agents", "list"])
    assert result.exit_code == 0, result.output
    assert "r_run" in result.output


def test_agents_spec_runnable_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/spec-runnable"
        return data_response({"items": [{"name": "r", "tool_name": "r_run", "spec_runnable": True}], "total": 1})

    result = run_cli(monkeypatch, handler, ["agents", "spec-runnable"])
    assert result.exit_code == 0, result.output
    assert "r_run" in result.output


def _agent_sse(request: httpx.Request) -> httpx.Response:
    body = 'data: {"type":"done"}\n\n'
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body.encode())


def test_agents_run_reads_input_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    input_file = tmp_path / "in.json"
    input_file.write_text('{"query":"weather"}')
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/researcher/runs"
        seen.update(json.loads(request.content))
        return _agent_sse(request)

    result = run_cli(monkeypatch, handler, ["agents", "run", "researcher", "--input-file", str(input_file)])
    assert result.exit_code == 0, result.output
    assert seen == {"query": "weather"}


def test_agents_run_reads_input_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _agent_sse(request)

    result = run_cli(
        monkeypatch,
        handler,
        ["agents", "run", "researcher", "--input-file", "-"],
        stdin='{"query":"weather"}',
    )
    assert result.exit_code == 0, result.output
    assert seen == {"query": "weather"}


def test_agents_run_rejects_both_input_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    input_file = tmp_path / "in.json"
    input_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _agent_sse,
        ["agents", "run", "researcher", "--input", "{}", "--input-file", str(input_file)],
    )
    assert result.exit_code != 0
    assert "--input-file" in result.output


def test_agents_run_rejects_neither_input_nor_file(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _agent_sse, ["agents", "run", "researcher"])
    assert result.exit_code != 0
    assert "--input" in result.output


def test_agents_authored_run_reads_input_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    input_file = tmp_path / "in.json"
    input_file.write_text('{"query":"weather"}')
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/authored/my_agent/runs"
        seen.update(json.loads(request.content))
        return _agent_sse(request)

    result = run_cli(monkeypatch, handler, ["agents", "authored-run", "my_agent", "--input-file", str(input_file)])
    assert result.exit_code == 0, result.output
    assert seen == {"query": "weather"}


def test_agents_authored_run_reads_input_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/agents/authored/my_agent/runs"
        seen.update(json.loads(request.content))
        return _agent_sse(request)

    result = run_cli(
        monkeypatch,
        handler,
        ["agents", "authored-run", "my_agent", "--input-file", "-"],
        stdin='{"query":"weather"}',
    )
    assert result.exit_code == 0, result.output
    assert seen == {"query": "weather"}


def test_agents_authored_run_rejects_both_input_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    input_file = tmp_path / "in.json"
    input_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _agent_sse,
        ["agents", "authored-run", "my_agent", "--input", "{}", "--input-file", str(input_file)],
    )
    assert result.exit_code != 0
    assert "--input-file" in result.output


def test_agents_authored_run_rejects_neither_input_nor_file(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _agent_sse, ["agents", "authored-run", "my_agent"])
    assert result.exit_code != 0
    assert "--input" in result.output
