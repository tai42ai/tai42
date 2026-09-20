"""``tai resources`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_resources_get_plain_uses_read_get(monkeypatch: pytest.MonkeyPatch) -> None:
    # No render vars -> the plain fetch-as-is path calls the read-classed GET door with
    # ``resource_id`` as a query param and no body.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/resources/get"
        assert request.url.params["resource_id"] == "doc.txt"
        assert not request.content
        return data_response("raw text")

    result = run_cli(monkeypatch, handler, ["resources", "get", "doc.txt"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == "raw text"


def test_resources_get_render_uses_write_post(monkeypatch: pytest.MonkeyPatch) -> None:
    # A render var -> the render path posts ``kwargs`` to the write-classed POST.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/resources/get"
        assert json.loads(request.content) == {"resource_id": "greet.j2", "kwargs": {"name": "Ada"}}
        return data_response("Hello Ada")

    result = run_cli(monkeypatch, handler, ["resources", "get", "greet.j2", "--kw", "name=Ada"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == "Hello Ada"


def _capture_resource_render(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/resources/get"
        seen.update(json.loads(request.content))
        return data_response({"content": "hi"})

    return handler


def test_resources_render_reads_kwargs_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text('{"name":"secret"}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_resource_render(seen),
        ["resources", "get", "prompts/greeting.md", "--kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["kwargs"] == {"name": "secret"}


def test_resources_render_reads_kwargs_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_resource_render(seen),
        ["resources", "get", "prompts/greeting.md", "--kwargs-file", "-"],
        stdin='{"name":"secret"}',
    )
    assert result.exit_code == 0, result.output
    assert seen["kwargs"] == {"name": "secret"}


def test_resources_render_rejects_both_kwargs_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    kwargs_file = tmp_path / "k.json"
    kwargs_file.write_text("{}")
    result = run_cli(
        monkeypatch,
        _capture_resource_render({}),
        ["resources", "get", "prompts/greeting.md", "--kwargs", "{}", "--kwargs-file", str(kwargs_file)],
    )
    assert result.exit_code != 0
    assert "--kwargs-file" in result.output
