"""The operator-send and mode CLI commands, exercised against a fake ``/api/*`` server: each
shapes its request to the door it @covers and renders the result.
"""

from __future__ import annotations

import json

import httpx
import pytest

from tests.remote_harness import data_response, error_response, run_cli, visible


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
