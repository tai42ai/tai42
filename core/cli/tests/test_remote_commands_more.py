"""Remote command groups that the primary suites do not otherwise exercise:
conversations, tool-meta, storage, checkpoints, roles, mcp, fleet, and presets.

Each command is a thin wrapper over one ``/api/*`` route: a handler asserts the
method/path/body the CLI sends and returns a canned envelope, and the command's
render is checked. Usage-error branches (mutually exclusive flags, malformed
input) assert a non-zero exit.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.remote_harness import data_response, run_cli

# -- conversations -----------------------------------------------------------


def test_conversations_list(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/conversations"
        return data_response({"items": [{"route_name": "chat"}]})

    result = run_cli(monkeypatch, handler, ["conversations", "list"])
    assert result.exit_code == 0, result.output
    assert "chat" in result.output


def test_conversations_get(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversations/chat"
        return data_response({"route_name": "chat"})

    result = run_cli(monkeypatch, handler, ["conversations", "get", "chat"])
    assert result.exit_code == 0, result.output


def test_conversations_create_builds_full_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/conversations/chat"
        body = json.loads(request.content)
        assert body["door"] == "channel"
        assert body["target_name"] == "relay"
        assert body["channel"] == "twilio"
        assert body["our_identity"] == "+15550001111"
        return data_response({"created": True})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "conversations",
            "create",
            "chat",
            "--door",
            "channel",
            "--target-name",
            "relay",
            "--execution-key",
            "svc",
            "--channel",
            "twilio",
            "--identity",
            "+15550001111",
        ],
    )
    assert result.exit_code == 0, result.output


def test_conversations_create_with_tool_target_maps_exprs(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["payload_expr"] == ".text"
        assert body["reply_expr"] == ".result"
        assert body["callback_url"] == "https://cb.example"
        return data_response({"created": False})

    result = run_cli(
        monkeypatch,
        handler,
        [
            "conversations",
            "create",
            "chat",
            "--door",
            "api",
            "--target-name",
            "echo",
            "--execution-key",
            "svc",
            "--target-kind",
            "tool",
            "--payload-expr",
            ".text",
            "--reply-expr",
            ".result",
            "--callback-url",
            "https://cb.example",
        ],
    )
    assert result.exit_code == 0, result.output


def test_conversations_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/conversations/chat"
        return data_response({"deleted": True})

    result = run_cli(monkeypatch, handler, ["conversations", "delete", "chat"])
    assert result.exit_code == 0, result.output


def test_conversations_delete_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        # The thread id rides the query, so an api-door id carrying a ``/`` reaches the door
        # spelled exactly as it was given.
        assert request.url.path == "/api/conversations/chat/thread"
        assert request.url.params["thread_id"] == "bridge:chat:+15550001111/user-7"
        return data_response({"removed": 1, "route_name": "chat", "thread_id": "bridge:chat:+15550001111/user-7"})

    result = run_cli(
        monkeypatch,
        handler,
        ["conversations", "delete-thread", "chat", "bridge:chat:+15550001111/user-7"],
    )
    assert result.exit_code == 0, result.output


def test_conversations_get_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversations/chat/messages/abc"
        return data_response({"message_id": "abc"})

    result = run_cli(monkeypatch, handler, ["conversations", "get-message", "chat", "abc"])
    assert result.exit_code == 0, result.output


def test_conversations_threads(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversations/chat/threads"
        assert request.url.params["page"] == "2"
        assert request.url.params["pageSize"] == "10"
        return data_response({"items": [{"thread_id": "t1"}]})

    result = run_cli(monkeypatch, handler, ["conversations", "threads", "chat", "--page", "2", "--page-size", "10"])
    assert result.exit_code == 0, result.output
    assert "t1" in result.output


def test_conversations_transcript(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversations/chat/transcript"
        assert request.url.params["thread_id"] == "bridge:chat:+1"
        assert request.url.params["order"] == "desc"
        return data_response({"items": [{"message_id": "m1"}]})

    result = run_cli(
        monkeypatch,
        handler,
        ["conversations", "transcript", "chat", "bridge:chat:+1", "--order", "desc"],
    )
    assert result.exit_code == 0, result.output


def test_conversations_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversations/messages/failed"
        return data_response({"items": [{"message_id": "m1"}]})

    result = run_cli(monkeypatch, handler, ["conversations", "failed"])
    assert result.exit_code == 0, result.output


def test_conversations_config_list_get_set_delete(monkeypatch: pytest.MonkeyPatch) -> None:
    def list_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversation-configs"
        return data_response({"items": [{"target_name": "assistant"}]})

    assert run_cli(monkeypatch, list_handler, ["conversations", "config-list"]).exit_code == 0

    def get_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversation-configs/agent/assistant"
        return data_response({"multichannel": True})

    assert run_cli(monkeypatch, get_handler, ["conversations", "config-get", "agent", "assistant"]).exit_code == 0

    def set_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/conversation-configs/agent/assistant"
        body = json.loads(request.content)
        assert body["multichannel"] is True
        assert body["greeting_template"] == "Hi {pairing_code}"
        return data_response({"created": True})

    assert (
        run_cli(
            monkeypatch,
            set_handler,
            [
                "conversations",
                "config-set",
                "agent",
                "assistant",
                "--multichannel",
                "--greeting-template",
                "Hi {pairing_code}",
            ],
        ).exit_code
        == 0
    )

    def delete_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/conversation-configs/agent/assistant"
        return data_response({"deleted": True})

    assert run_cli(monkeypatch, delete_handler, ["conversations", "config-delete", "agent", "assistant"]).exit_code == 0


# -- tool-meta ---------------------------------------------------------------


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


# -- storage -----------------------------------------------------------------


def test_storage_info_list_stat(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(monkeypatch, lambda r: data_response({"provider": "local"}), ["storage", "info"]).exit_code == 0

    def list_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/resources"
        return data_response({"resources": ["a.txt"]})

    assert run_cli(monkeypatch, list_handler, ["storage", "list"]).exit_code == 0

    def stat_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/resources/a.txt/stat"
        return data_response({"content_type": "text/plain"})

    assert run_cli(monkeypatch, stat_handler, ["storage", "stat", "a.txt"]).exit_code == 0


def test_storage_download_streams_raw_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/resources/a.txt/content"
        return httpx.Response(200, content=b"hello bytes")

    result = run_cli(monkeypatch, handler, ["storage", "download", "a.txt"], json_output=False)
    assert result.exit_code == 0, result.output
    assert "hello bytes" in result.output


def test_storage_upload_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        body = json.loads(request.content)
        assert body == {"id": "a.txt", "content_text": "hi"}
        return data_response({"id": "a.txt"})

    assert run_cli(monkeypatch, handler, ["storage", "upload", "a.txt", "--text", "hi"]).exit_code == 0


def test_storage_upload_base64(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"id": "a.bin", "content_base64": "AAAA"}
        return data_response({"id": "a.bin"})

    assert run_cli(monkeypatch, handler, ["storage", "upload", "a.bin", "--base64", "AAAA"]).exit_code == 0


def test_storage_upload_rejects_both_or_neither(monkeypatch: pytest.MonkeyPatch) -> None:
    both = run_cli(monkeypatch, lambda r: data_response({}), ["storage", "upload", "a", "--text", "x", "--base64", "y"])
    assert both.exit_code != 0
    neither = run_cli(monkeypatch, lambda r: data_response({}), ["storage", "upload", "a"])
    assert neither.exit_code != 0


def test_storage_delete_and_delete_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    def del_handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/storage/resources/a.txt"
        return data_response({"deleted": True})

    assert run_cli(monkeypatch, del_handler, ["storage", "delete", "a.txt"]).exit_code == 0

    def dir_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/storage/dirs/notes"
        return data_response({"deleted": True})

    assert run_cli(monkeypatch, dir_handler, ["storage", "delete-dir", "notes"]).exit_code == 0


# -- checkpoints -------------------------------------------------------------


def test_checkpoints_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/checkpoints/sweep"
        return data_response({"deleted": 3})

    assert run_cli(monkeypatch, handler, ["checkpoints", "sweep"]).exit_code == 0


# -- roles -------------------------------------------------------------------


def test_roles_show_filters_the_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/roles"
        return data_response([{"name": "editor"}, {"name": "viewer"}])

    result = run_cli(monkeypatch, handler, ["roles", "show", "editor"])
    assert result.exit_code == 0, result.output
    assert "editor" in result.output


def test_roles_show_unknown_role_is_usage_error(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(monkeypatch, lambda r: data_response([{"name": "editor"}]), ["roles", "show", "nope"])
    assert result.exit_code != 0


def test_roles_create_parses_grants(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["grants"] == {"presets": "write", "hooks": "read"}
        return data_response({"name": "ops"})

    result = run_cli(
        monkeypatch,
        handler,
        ["roles", "create", "ops", "--base-tier", "editor", "--grant", "presets=write", "--grant", "hooks=read"],
    )
    assert result.exit_code == 0, result.output


def test_roles_create_rejects_malformed_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    result = run_cli(
        monkeypatch,
        lambda r: data_response({}),
        ["roles", "create", "ops", "--base-tier", "editor", "--grant", "badgrant"],
    )
    assert result.exit_code != 0


def test_roles_edit_sends_only_passed_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/auth/roles/ops"
        assert json.loads(request.content) == {"description": "new"}
        return data_response({"name": "ops"})

    assert run_cli(monkeypatch, handler, ["roles", "edit", "ops", "--description", "new"]).exit_code == 0


def test_roles_versions_and_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    def versions_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/roles/ops/versions"
        return data_response({"versions": []})

    assert run_cli(monkeypatch, versions_handler, ["roles", "versions", "ops"]).exit_code == 0

    def rollback_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/auth/roles/ops/rollback"
        assert json.loads(request.content) == {"version": 2}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, rollback_handler, ["roles", "rollback", "ops", "2"]).exit_code == 0


# -- mcp ---------------------------------------------------------------------


def test_mcp_status_schema_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    assert run_cli(monkeypatch, lambda r: data_response({"servers": []}), ["mcp", "status"]).exit_code == 0
    assert run_cli(monkeypatch, lambda r: data_response({"type": "object"}), ["mcp", "schema"]).exit_code == 0

    def failed_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/mcp-status/failed"
        assert request.url.params["targets"] == "serve-1"
        return data_response({"failed": []})

    assert run_cli(monkeypatch, failed_handler, ["mcp", "failed", "--target", "serve-1"]).exit_code == 0


def test_mcp_set_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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


def test_mcp_set_rejects_wrong_shape(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
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


# -- fleet -------------------------------------------------------------------


def test_fleet_info(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/backend"
        return data_response({"installed": False})

    assert run_cli(monkeypatch, handler, ["fleet", "info"]).exit_code == 0


def test_fleet_workers_table_projects_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/fleet/workers"
        return data_response(
            {
                "workers": [
                    {
                        "name": "serve-1",
                        "kind": "serve",
                        "pid": 10,
                        "generation": 2,
                        "state": "up",
                        "stale": True,
                        "beat_at": "2000-01-01T00:00:00+00:00",
                        "last_op": {"op": "reload", "outcome": "ok"},
                    }
                ]
            }
        )

    result = run_cli(monkeypatch, handler, ["fleet", "workers"])
    assert result.exit_code == 0, result.output
    assert "serve-1" in result.output
    assert "(stale)" in result.output
    assert "reload:ok" in result.output


def test_fleet_workers_json_emits_raw_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response({"workers": [{"name": "serve-1"}]})

    result = run_cli(monkeypatch, handler, ["fleet", "workers"], json_output=True)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {"workers": [{"name": "serve-1"}]}


def test_fleet_workers_missing_beat_and_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response({"workers": [{"name": "serve-1", "beat_at": None, "last_op": None}]})

    result = run_cli(monkeypatch, handler, ["fleet", "workers"])
    assert result.exit_code == 0, result.output
    assert "—" in result.output


def test_fleet_workers_unparseable_beat(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response({"workers": [{"name": "serve-1", "beat_at": "not-a-date"}]})

    assert run_cli(monkeypatch, handler, ["fleet", "workers"]).exit_code == 0


def test_fleet_reload_config(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/fleet/reload-config"
        assert json.loads(request.content) == {"targets": ["serve-1"]}
        return data_response({"report": []})

    assert run_cli(monkeypatch, handler, ["fleet", "reload-config", "--target", "serve-1"]).exit_code == 0


# -- presets (rename / referees / validate / set-version-tags) ---------------


def test_presets_rename(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/old/rename"
        assert json.loads(request.content) == {"new_name": "new"}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["presets", "rename", "old", "new"]).exit_code == 0


def test_presets_referees(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/p/referees"
        return data_response({"referees": []})

    assert run_cli(monkeypatch, handler, ["presets", "referees", "p"]).exit_code == 0


def test_presets_validate_builds_body(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/presets/validate"
        body = json.loads(request.content)
        assert body["name"] == "greet"
        assert body["base_tool"] == "echo"
        assert body["fixed_kwargs"] == {"prefix": "hi"}
        return data_response({"verdict": "create"})

    result = run_cli(
        monkeypatch,
        handler,
        ["presets", "validate", "greet", "--base-tool", "echo", "--kwargs", '{"prefix":"hi"}'],
    )
    assert result.exit_code == 0, result.output


def test_presets_set_version_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/presets/p/versions/2/tags"
        assert json.loads(request.content) == {"tags": ["stable"]}
        return data_response({"ok": True})

    assert run_cli(monkeypatch, handler, ["presets", "set-version-tags", "p", "2", "stable"]).exit_code == 0


# -- manifest (tools/agents entries + api-tools) -----------------------------


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
