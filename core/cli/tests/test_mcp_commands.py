"""``tai mcp`` command group exercised against a fake ``/api/*`` server."""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli, strip_ansi, visible


def test_mcp_set_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps([{"title": "srv"}]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-config"
        assert json.loads(request.content) == {"mcp": [{"title": "srv"}]}
        return data_response({"reloaded": True})

    result = run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(config)])
    assert result.exit_code == 0, result.output


def test_mcp_status(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status"
        return data_response({"bindings": []})

    result = run_cli(monkeypatch, handler, ["mcp", "status"])
    assert result.exit_code == 0, result.output


def test_mcp_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-config/schema"
        return data_response({"type": "object"})

    result = run_cli(monkeypatch, handler, ["mcp", "schema"])
    assert result.exit_code == 0, result.output


def test_mcp_set_from_object_with_mcp_key(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"mcp": [{"title": "srv"}]}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"mcp": [{"title": "srv"}]}
        return data_response({"reloaded": True})

    result = run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(config)])
    assert result.exit_code == 0, result.output


def test_mcp_set_rejects_malformed_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text("{not json", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(config)])
    assert result.exit_code != 0
    assert "valid JSON" in result.output


def test_mcp_set_rejects_wrong_shape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    config = tmp_path / "mcp.json"
    config.write_text(json.dumps({"other": 1}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - never reached
        return data_response({})

    result = run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(config)])
    assert result.exit_code != 0
    assert "JSON list" in result.output


def test_mcp_reload_by_title(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/mcp-status/my-server/reload"
        return data_response({"reloaded": True})

    result = run_cli(monkeypatch, handler, ["mcp", "reload", "my-server"])
    assert result.exit_code == 0, result.output


def test_mcp_status_schema_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(monkeypatch, lambda r: data_response({"servers": []}), ["mcp", "status"]).exit_code == 0
    assert run_cli(monkeypatch, lambda r: data_response({"type": "object"}), ["mcp", "schema"]).exit_code == 0

    def failed_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status/failed"
        assert request.url.params["targets"] == "serve-1"
        return data_response({"failed": []})

    assert run_cli(monkeypatch, failed_handler, ["mcp", "failed", "--target", "serve-1"]).exit_code == 0


def test_mcp_set_from_object_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcp": [{"title": "srv"}]}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/mcp-config"
        assert json.loads(request.content) == {"mcp": [{"title": "srv"}]}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(cfg)]).exit_code == 0


def test_mcp_set_accepts_bare_array(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps([{"title": "srv"}]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"mcp": [{"title": "srv"}]}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "set", "--file", str(cfg)]).exit_code == 0


def test_mcp_set_rejects_bad_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{not json", encoding="utf-8")
    assert run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "set", "--file", str(cfg)]).exit_code != 0


def test_mcp_set_rejects_wrong_shape_nonzero_exit(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    assert run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "set", "--file", str(cfg)]).exit_code != 0


def test_mcp_add_single_object(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps({"title": "srv"}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/mcp-config/entries"
        assert json.loads(request.content) == {"entries": [{"title": "srv"}], "replace": False}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "add", "--file", str(cfg)]).exit_code == 0


def test_mcp_add_bare_array_keeps_order(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps([{"title": "a"}, {"title": "b"}]), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"entries": [{"title": "a"}, {"title": "b"}], "replace": False}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "add", "--file", str(cfg)]).exit_code == 0


def test_mcp_add_entries_object_with_replace(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps({"entries": [{"title": "srv"}]}), encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {"entries": [{"title": "srv"}], "replace": True}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "add", "--file", str(cfg), "--replace"]).exit_code == 0


def test_mcp_add_rejects_bad_json(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text("{not json", encoding="utf-8")
    assert run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "add", "--file", str(cfg)]).exit_code != 0


def test_mcp_add_rejects_wrong_shape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps(42), encoding="utf-8")
    result = run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "add", "--file", str(cfg)])
    assert result.exit_code != 0
    assert "entries" in result.output


def test_mcp_add_rejects_non_list_entries(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # An "entries" key whose value is not a list is a malformed wrapper, refused
    # locally by name — never forwarded as a non-list body.
    cfg = tmp_path / "entries.json"
    cfg.write_text(json.dumps({"entries": "srv"}), encoding="utf-8")
    result = run_cli(monkeypatch, lambda r: data_response({}), ["mcp", "add", "--file", str(cfg)])
    assert result.exit_code != 0
    assert "entries" in result.output


def test_mcp_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/mcp-config/entries/srv"
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["mcp", "remove", "srv"]).exit_code == 0


def test_mcp_reload_reload_failed_deregister(monkeypatch: pytest.MonkeyPatch) -> None:
    def reload_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status/srv/reload"
        assert json.loads(request.content) == {"targets": ["serve-1"]}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, reload_handler, ["mcp", "reload", "srv", "--target", "serve-1"]).exit_code == 0

    def reload_failed_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status/reload-failed"
        assert json.loads(request.content) == {"targets": None}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, reload_failed_handler, ["mcp", "reload-failed"]).exit_code == 0

    def deregister_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status/srv/deregister"
        return data_response({"ok": True})

    assert run_cli(monkeypatch, deregister_handler, ["mcp", "deregister", "srv"]).exit_code == 0


_ADVERTISED = {
    "title": "srv",
    "include": ["srv_read", "srv_gone"],
    "exclude": [],
    "tools": [
        {
            "name": "srv_read",
            "title": None,
            "description": "Read a record.",
            "selected": True,
            "bound": True,
            "unbound_reason": None,
        },
        {
            "name": "srv_write",
            "title": None,
            "description": "Write a record.",
            "selected": False,
            "bound": False,
            "unbound_reason": None,
        },
    ],
    "unadvertised": ["srv_gone"],
}


def _advertised_handler(request: httpx.Request) -> httpx.Response:
    assert request.method == "POST"
    assert request.url.raw_path == b"/api/mcp-status/my%20srv/advertised-tools"
    assert request.content == b""
    return data_response(_ADVERTISED)


def test_mcp_tools_prints_each_advertised_tool_then_the_unadvertised_names(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _advertised_handler, ["mcp", "tools", "my srv"])

    assert result.exit_code == 0, result.output
    lines = [line.split() for line in strip_ansi(result.output).splitlines()]
    assert lines[0] == ["name", "selected", "bound", "unbound_reason", "description"]
    assert lines[2] == ["srv_read", "true", "true", "Read", "a", "record."]
    assert lines[3] == ["srv_write", "false", "false", "Write", "a", "record."]
    assert lines[4] == ["srv_gone", "not", "advertised"]
    assert len(lines) == 5


def test_mcp_tools_json_emits_the_whole_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, _advertised_handler, ["mcp", "tools", "my srv"], json_output=True)

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == _ADVERTISED


@pytest.mark.parametrize(
    ("args", "body"),
    [
        (["--include", "srv_read", "--include", "srv_list"], {"include": ["srv_read", "srv_list"], "exclude": []}),
        (["--exclude", "srv_write"], {"include": [], "exclude": ["srv_write"]}),
        (["--all"], {"include": [], "exclude": []}),
    ],
)
def test_mcp_pick_puts_the_one_selection_mode(
    monkeypatch: pytest.MonkeyPatch, args: list[str], body: dict[str, list[str]]
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.raw_path == b"/api/mcp-config/entries/my%20srv/tool-filter"
        assert json.loads(request.content) == body
        return data_response({"ok": True})

    result = run_cli(monkeypatch, handler, ["mcp", "pick", "my srv", *args])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--include", "srv_read", "--exclude", "srv_write"],
        ["--include", "srv_read", "--all"],
        ["--exclude", "srv_write", "--all"],
    ],
)
def test_mcp_pick_needs_exactly_one_mode(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request is sent without exactly one mode")

    result = run_cli(monkeypatch, handler, ["mcp", "pick", "srv", *args])
    assert result.exit_code != 0
    assert "give --include, --exclude or --all" in visible(result.output)


def test_mcp_pick_refused_for_a_held_tool_exits_non_zero_with_the_holders(monkeypatch: pytest.MonkeyPatch) -> None:
    message = (
        "tool selection of 'srv' cannot unpick 'srv_write' — held by:\n"
        "- preset 'nightly', which uses 'srv_write'\n"
        "remove those references first, or keep those tools picked"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return error_response(message, 409)

    result = run_cli(monkeypatch, handler, ["mcp", "pick", "srv", "--include", "srv_read"])
    assert result.exit_code == 1
    assert "- preset 'nightly', which uses 'srv_write'" in result.output
    assert "cannot unpick 'srv_write'" in result.output
