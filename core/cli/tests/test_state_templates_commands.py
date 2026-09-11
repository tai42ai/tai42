"""The ``tai state-templates`` remote command group, exercised against a fake
``/api/state-templates*`` server: each command shapes its request to the door it
@covers, the document commands read their body from ``--data`` / ``--file`` / stdin,
and typed errors surface as a non-zero exit carrying the server message.
"""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli, visible


def test_list_gets_the_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/state-templates"
        assert request.headers["x-api-key"] == "test-key"
        return data_response([{"name": "counters"}])

    result = run_cli(monkeypatch, handler, ["state-templates", "list"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [{"name": "counters"}]


def test_get_reads_one_module(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/state-templates/counters"
        return data_response({"name": "counters", "schema": {"type": "object"}})

    result = run_cli(monkeypatch, handler, ["state-templates", "get", "counters"])
    assert result.exit_code == 0, result.output
    assert "counters" in result.output


def test_put_reads_data_and_omits_replace(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/state-templates/counters"
        # No --replace given: the door is called without the replace flag.
        assert "replace" not in request.url.params
        assert json.loads(request.content) == {"schema": {"type": "object"}}
        return data_response({"name": "counters"})

    result = run_cli(
        monkeypatch,
        handler,
        ["state-templates", "put", "counters", "--data", '{"schema": {"type": "object"}}'],
    )
    assert result.exit_code == 0, result.output


def test_put_replace_sets_the_query_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["replace"] == "true"
        return data_response({"name": "counters"})

    result = run_cli(
        monkeypatch,
        handler,
        ["state-templates", "put", "counters", "--data", "{}", "--replace"],
    )
    assert result.exit_code == 0, result.output


def test_put_reads_the_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    doc = tmp_path / "module.json"
    doc.write_text('{"schema": {"type": "object"}}')

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"schema": {"type": "object"}}
        return data_response({"name": "counters"})

    result = run_cli(monkeypatch, handler, ["state-templates", "put", "counters", "--file", str(doc)])
    assert result.exit_code == 0, result.output


def test_put_reads_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"schema": {}}
        return data_response({"name": "counters"})

    result = run_cli(
        monkeypatch,
        handler,
        ["state-templates", "put", "counters"],
        stdin='{"schema": {}}',
    )
    assert result.exit_code == 0, result.output


def test_put_rejects_both_data_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    doc = tmp_path / "module.json"
    doc.write_text("{}")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made when both sources are given")

    result = run_cli(
        monkeypatch,
        handler,
        ["state-templates", "put", "counters", "--data", "{}", "--file", str(doc)],
    )
    assert result.exit_code != 0
    assert "only one of --data / --file" in visible(result.output)


def test_put_rejects_empty_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be made when stdin is empty")

    result = run_cli(monkeypatch, handler, ["state-templates", "put", "counters"], stdin="   \n")
    assert result.exit_code != 0
    assert "no document on stdin" in visible(result.output)


def test_delete_hits_the_module_door(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/state-templates/counters"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["state-templates", "delete", "counters"])
    assert result.exit_code == 0, result.output


def test_delete_surfaces_an_attached_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("module 'counters' is attached on state 'status'", 409)

    result = run_cli(monkeypatch, handler, ["state-templates", "delete", "counters"])
    assert result.exit_code != 0
    assert "is attached on state" in visible(result.output)
