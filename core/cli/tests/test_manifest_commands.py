"""``tai manifest`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, run_cli


def test_manifest_show(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/manifest"
        return data_response({"mcp": [], "user_tools": ["a"]})

    result = run_cli(monkeypatch, handler, ["manifest", "show"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"mcp": [], "user_tools": ["a"]}


def test_manifest_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/plugins"
        return data_response([{"name": "studio-x"}])

    result = run_cli(monkeypatch, handler, ["manifest", "plugins"])
    assert result.exit_code == 0, result.output
    assert "studio-x" in result.output


def test_manifest_replace_posts_text_verbatim_with_markers_intact(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # The command posts the manifest TEXT verbatim — ``!ENV`` markers are NEVER
    # resolved client-side (a first-party client resolving a secret before a
    # persist-through replace would bake it to disk); the server owns resolution.
    raw = "backend_module: !ENV ${TAI_BACKEND}\nmcp: []\n"
    manifest_file = tmp_path / "manifest.yml"
    manifest_file.write_text(raw, encoding="utf-8")
    monkeypatch.setenv("TAI_BACKEND", "myapp.backend")  # would resolve IF the client parsed it

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/manifest/replace"
        body = json.loads(request.content)
        # The exact file text, marker intact — no resolved value, no ``targets``.
        assert body == {"manifest_text": raw}
        assert "myapp.backend" not in request.content.decode()
        return data_response({"status": "ok"})

    result = run_cli(monkeypatch, handler, ["manifest", "replace", "--file", str(manifest_file)])
    assert result.exit_code == 0, result.output


def test_manifest_tools_add(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps({"title": "grp"}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/tools-config/entries"
        assert json.loads(request.content) == {"entries": [{"title": "grp"}], "replace": True}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["manifest", "tools-add", "--file", str(cfg), "--replace"]).exit_code == 0


def test_manifest_tools_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/tools-config/entries/grp"
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["manifest", "tools-remove", "grp"]).exit_code == 0


def test_manifest_agents_add(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps([{"title": "grp"}]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/agents-config/entries"
        assert json.loads(request.content) == {"entries": [{"title": "grp"}], "replace": False}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["manifest", "agents-add", "--file", str(cfg)]).exit_code == 0


def test_manifest_agents_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/agents-config/entries/grp"
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["manifest", "agents-remove", "grp"]).exit_code == 0


def test_manifest_api_tools_builds_all_four_lists(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/api-tools"
        assert json.loads(request.content) == {
            "include_add": ["echo"],
            "include_remove": ["status"],
            "exclude_add": ["alerts"],
            "exclude_remove": ["events"],
        }
        return data_response({"ok": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "manifest",
            "api-tools",
            "--include-add",
            "echo",
            "--include-remove",
            "status",
            "--exclude-add",
            "alerts",
            "--exclude-remove",
            "events",
        ],
    )
    assert result.exit_code == 0, result.output


def test_manifest_api_tools_requires_a_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(monkeypatch, lambda r: data_response({}), ["manifest", "api-tools"]).exit_code != 0
