"""``tai tool-meta`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_tool_meta_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tool-meta"
        return data_response({"folders": [], "tools": []})

    assert run_cli(monkeypatch, handler, ["tool-meta", "list"]).exit_code == 0


def test_tool_meta_set_sends_only_passed_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/api/tool-meta/tools/web_search"
        body = json.loads(request.content)
        assert body == {"display_name": "Web Search", "tags": ["research"], "hidden": True}
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "tool-meta",
            "set",
            "web_search",
            "--display-name",
            "Web Search",
            "--tag",
            "research",
            "--visibility",
            "hidden",
        ],
    )
    assert result.exit_code == 0, result.output


def test_tool_meta_set_clear_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"display_name": None, "folder_id": None, "tags": []}
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        ["tool-meta", "set", "web_search", "--clear-display-name", "--clear-folder", "--clear-tags"],
    )
    assert result.exit_code == 0, result.output


def test_tool_meta_set_sends_badges(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        # Repeatable --badge replaces the whole set; only the badges field is sent.
        assert body == {"badges": ["storage-read", "network"]}
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        ["tool-meta", "set", "web_search", "--badge", "storage-read", "--badge", "network"],
    )
    assert result.exit_code == 0, result.output


def test_tool_meta_set_clear_badges(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"badges": []}
        return data_response({"ok": True})

    result = run_cli(monkeypatch, handler, ["tool-meta", "set", "web_search", "--clear-badges"])
    assert result.exit_code == 0, result.output


def test_tool_meta_set_rejects_conflicting_badge_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        monkeypatch,
        lambda r: data_response({}),
        ["tool-meta", "set", "web_search", "--badge", "llm", "--clear-badges"],
    )
    assert result.exit_code != 0


def test_tool_meta_set_rejects_conflicting_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        monkeypatch,
        lambda r: data_response({}),
        ["tool-meta", "set", "web_search", "--display-name", "X", "--clear-display-name"],
    )
    assert result.exit_code != 0


def test_tool_meta_set_rejects_bad_visibility(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, lambda r: data_response({}), ["tool-meta", "set", "t", "--visibility", "nope"])
    assert result.exit_code != 0


def test_tool_meta_set_requires_at_least_one_field(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, lambda r: data_response({}), ["tool-meta", "set", "t"])
    assert result.exit_code != 0


def test_tool_meta_delete_and_folders(monkeypatch: pytest.MonkeyPatch) -> None:
    def delete_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/tool-meta/tools/web_search"
        return data_response({"deleted": True})

    assert run_cli(monkeypatch, delete_handler, ["tool-meta", "delete", "web_search"]).exit_code == 0

    def create_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tool-meta/folders"
        body = json.loads(request.content)
        assert body == {"name": "Research", "parent_id": "root"}
        return data_response({"id": "f1"})

    assert (
        run_cli(monkeypatch, create_handler, ["tool-meta", "folder-create", "Research", "--parent", "root"]).exit_code
        == 0
    )

    def rename_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tool-meta/folders/f1/rename"
        assert json.loads(request.content) == {"name": "Archive"}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, rename_handler, ["tool-meta", "folder-rename", "f1", "Archive"]).exit_code == 0

    def move_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tool-meta/folders/f1/move"
        assert json.loads(request.content) == {"parent_id": None}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, move_handler, ["tool-meta", "folder-move", "f1"]).exit_code == 0

    def fdelete_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/tool-meta/folders/f1"
        return data_response({"deleted": True})

    assert run_cli(monkeypatch, fdelete_handler, ["tool-meta", "folder-delete", "f1"]).exit_code == 0
