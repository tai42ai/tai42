"""The ``tai templates`` command group, exercised against a fake ``/api/*template*``
server that models the store's file/directory id space.

The composed path a live upload takes: an id occupied by a directory that still
holds templates is refused, deleting the last child frees the id, and a re-upload
at the freed id then succeeds.
"""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli


class _FakeStore:
    """A minimal template store keyed like the real backends: one id space where a
    file id may not also name a directory that still holds templates."""

    def __init__(self, keys: dict[str, str]) -> None:
        self.keys = dict(keys)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        if request.url.path == "/api/upload-template":
            path = body["path"]
            under = sorted(k for k in self.keys if k.startswith(f"{path}/"))
            if under:
                return error_response(
                    f"cannot upload template {path!r}: templates exist under that path: "
                    f"{', '.join(repr(k) for k in under)}; delete them first",
                    409,
                )
            self.keys[path] = body["content"]
            return data_response({"path": path, "uploaded": True})
        if request.url.path == "/api/delete-template":
            self.keys.pop(body["path"], None)
            return data_response({"path": body["path"], "deleted": True})
        raise AssertionError(f"unexpected request to {request.url.path}")


def test_delete_then_upload_at_freed_id(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    store = _FakeStore({"a/b/c.j2": "child"})
    source = tmp_path / "new.j2"
    source.write_text("i am the new a/b", encoding="utf-8")

    # The id "a/b" is occupied by a directory holding "a/b/c.j2": the upload is refused.
    refused = run_cli(monkeypatch, store, ["templates", "upload", "a/b", "--file", str(source)])
    assert refused.exit_code != 0
    assert "delete them first" in refused.output

    # Delete the last child, freeing the id.
    freed = run_cli(monkeypatch, store, ["templates", "delete", "a/b/c.j2"])
    assert freed.exit_code == 0, freed.output

    # The re-upload at the freed id now succeeds.
    uploaded = run_cli(monkeypatch, store, ["templates", "upload", "a/b", "--file", str(source)])
    assert uploaded.exit_code == 0, uploaded.output
    assert store.keys["a/b"] == "i am the new a/b"


def test_templates_render_requires_a_text_source(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["templates", "render"])
    assert result.exit_code != 0
    assert "--text" in result.output


def test_templates_render_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["text"] == {"id": "greet", "kwargs": {"name": "Ada"}}
        return data_response({"rendered": "hi Ada"})

    result = run_cli(
        monkeypatch,
        handler,
        ["templates", "render", "--text", '{"id": "greet", "kwargs": {"name": "Ada"}}'],
    )
    assert result.exit_code == 0, result.output


def test_templates_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/templates"
        return data_response({"templates": ["prompts/greeting.md"]})

    result = run_cli(monkeypatch, handler, ["templates", "list"])
    assert result.exit_code == 0, result.output
    assert "greeting" in result.output


def test_templates_get_posts_id(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/template"
        assert json.loads(request.content) == {"template_id": "prompts/greeting.md"}
        return data_response({"content": "Hi {{ name }}"})

    result = run_cli(monkeypatch, handler, ["templates", "get", "prompts/greeting.md"])
    assert result.exit_code == 0, result.output


def test_templates_upload_sends_file_content(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    local = tmp_path / "greeting.md"
    local.write_text("Hello {{ name }}", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/upload-template"
        assert json.loads(request.content) == {"path": "prompts/greeting.md", "content": "Hello {{ name }}"}
        return data_response({"written": True})

    result = run_cli(monkeypatch, handler, ["templates", "upload", "prompts/greeting.md", "--file", str(local)])
    assert result.exit_code == 0, result.output


def test_templates_delete_posts_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/delete-template"
        assert json.loads(request.content) == {"path": "prompts/greeting.md"}
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["templates", "delete", "prompts/greeting.md"])
    assert result.exit_code == 0, result.output


def test_templates_delete_dir_posts_path(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/delete-template-dir"
        assert json.loads(request.content) == {"path": "prompts/archive"}
        return data_response({"path": "prompts/archive", "deleted": True})

    result = run_cli(monkeypatch, handler, ["templates", "delete-dir", "prompts/archive"])
    assert result.exit_code == 0, result.output


def test_templates_render_by_inline_content(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["text"] == {"content": "Hi {{ name }}", "kwargs": {"name": "Ada"}}
        return data_response({"rendered": "Hi Ada"})

    result = run_cli(
        monkeypatch,
        handler,
        ["templates", "render", "--text", '{"content": "Hi {{ name }}", "kwargs": {"name": "Ada"}}'],
    )
    assert result.exit_code == 0, result.output


def test_templates_clear_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/clear-templates-cache"
        return data_response({"cleared": True})

    result = run_cli(monkeypatch, handler, ["templates", "clear-cache"])
    assert result.exit_code == 0, result.output


def _capture_render_template(seen: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/render-template"
        seen.update(json.loads(request.content))
        return data_response({"rendered": "hi"})

    return handler


def test_templates_render_reads_text_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    text_file = tmp_path / "t.json"
    text_file.write_text('{"id": "prompts/greeting.md", "kwargs": {"name": "secret"}}')
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_render_template(seen),
        ["templates", "render", "--text-file", str(text_file)],
    )
    assert result.exit_code == 0, result.output
    assert seen["text"] == {"id": "prompts/greeting.md", "kwargs": {"name": "secret"}}


def test_templates_render_reads_text_from_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}
    result = run_cli(
        monkeypatch,
        _capture_render_template(seen),
        ["templates", "render", "--text-file", "-"],
        stdin='{"id": "prompts/greeting.md", "kwargs": {"name": "secret"}}',
    )
    assert result.exit_code == 0, result.output
    assert seen["text"] == {"id": "prompts/greeting.md", "kwargs": {"name": "secret"}}


def test_templates_render_rejects_both_text_and_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    text_file = tmp_path / "t.json"
    text_file.write_text('{"content": "hi"}')
    result = run_cli(
        monkeypatch,
        _capture_render_template({}),
        [
            "templates",
            "render",
            "--text",
            '{"content": "hi"}',
            "--text-file",
            str(text_file),
        ],
    )
    assert result.exit_code != 0
    assert "--text-file" in result.output
