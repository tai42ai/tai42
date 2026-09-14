"""The operator-send and mode CLI commands, exercised against a fake ``/api/*`` server: each
shapes its request to the door it @covers and renders the result.
"""

from __future__ import annotations

import json

import httpx
import pytest

from .remote_harness import data_response, error_response, run_cli, visible


def test_create_carries_the_initial_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/conversations/chat"
        body = json.loads(request.content)
        assert body["initial_mode"] == "manual"
        return data_response({"created": True, "route_name": "chat", "route": {}, "callback_secret": None})

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
            "relay",
            "--execution-key",
            "svc",
            "--callback-url",
            "https://cb.example/x",
            "--initial-mode",
            "manual",
        ],
    )
    assert result.exit_code == 0, result.output


def test_send_posts_thread_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/conversations/chat/thread/messages"
        body = json.loads(request.content)
        assert body == {"thread_id": "bridge:chat:+1", "text": "on it"}
        return data_response({"message_id": "m1", "thread_id": "bridge:chat:+1"})

    result = run_cli(monkeypatch, handler, ["conversations", "send", "chat", "bridge:chat:+1", "--text", "on it"])
    assert result.exit_code == 0, result.output
    assert "m1" in result.output


def test_send_passes_the_optional_address(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body == {"thread_id": "bridge:@person:p1", "text": "hi", "address": "+2000"}
        return data_response({"message_id": "m2", "thread_id": "bridge:@person:p1"})

    result = run_cli(
        monkeypatch,
        handler,
        ["conversations", "send", "chat", "bridge:@person:p1", "--text", "hi", "--address", "+2000"],
    )
    assert result.exit_code == 0, result.output


def test_send_surfaces_a_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("thread_id 'x' is not a thread of route 'chat'", 400)

    result = run_cli(monkeypatch, handler, ["conversations", "send", "chat", "x", "--text", "hi"])
    assert result.exit_code != 0
    assert "not a thread of route" in visible(result.output)


def test_mode_get_reads_the_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/conversations/chat/thread/mode"
        assert request.url.params["thread_id"] == "bridge:chat:+1"
        return data_response({"mode": "manual", "source": "thread"})

    result = run_cli(monkeypatch, handler, ["conversations", "mode-get", "chat", "bridge:chat:+1"])
    assert result.exit_code == 0, result.output
    assert "manual" in result.output


def test_mode_set_puts_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/conversations/chat/thread/mode"
        body = json.loads(request.content)
        assert body == {"thread_id": "bridge:chat:+1", "mode": "manual"}
        return data_response(
            {"route_name": "chat", "thread_id": "bridge:chat:+1", "mode": "manual", "source": "thread"}
        )

    result = run_cli(monkeypatch, handler, ["conversations", "mode-set", "chat", "bridge:chat:+1", "manual"])
    assert result.exit_code == 0, result.output
    assert "manual" in result.output


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
        assert body["payload_expr"] == {"content": ".text"}
        assert body["reply_expr"] == {"content": ".result"}
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


def test_conversations_create_sets_turns_per_hour_override(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["turns_per_hour_override"] == 6000
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
            "--turns-per-hour-override",
            "6000",
        ],
    )
    assert result.exit_code == 0, result.output


def test_conversations_create_omits_turns_per_hour_override_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "turns_per_hour_override" not in body
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


def test_conversations_create_sets_error_reply_text(monkeypatch: pytest.MonkeyPatch) -> None:
    text = "Lo sentimos, algo salió mal. Inténtalo de nuevo."

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["error_reply_text"] == text
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
            "--error-reply-text",
            text,
        ],
    )
    assert result.exit_code == 0, result.output


def test_conversations_create_omits_error_reply_text_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "error_reply_text" not in body
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


def test_conversations_create_sets_locale(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["locale"] == "fr"
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
            "--locale",
            "fr",
        ],
    )
    assert result.exit_code == 0, result.output


def test_conversations_create_omits_locale_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "locale" not in body
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
