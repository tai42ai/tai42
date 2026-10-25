"""The operator-send and mode CLI commands, exercised against a fake ``/api/*`` server: each
shapes its request to the door it @covers and renders the result.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tai42_contract.conversations.routes import ReplyTexts

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
        assert body["start_expr"] == {"content": ".text"}
        assert body["cancel_expr"] == {"content": "$parked | map(.id)"}
        assert body["resume_expr"] == {"content": "null"}
        assert body["extras_expr"] == {"content": "{warm_start: .text}"}
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
            "--start-expr",
            ".text",
            "--cancel-expr",
            "$parked | map(.id)",
            "--resume-expr",
            "null",
            "--extras-expr",
            "{warm_start: .text}",
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


_CHANNEL_ROUTE_ARGS = [
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
]

_REPLY_FLAGS = [
    ("--reply-error", "error"),
    ("--reply-slow-down", "slow_down"),
    ("--reply-pairing-linked", "pairing_linked"),
    ("--reply-pairing-unlinked", "pairing_unlinked"),
    ("--reply-pairing-not-linked", "pairing_not_linked"),
    ("--reply-pairing-code-invalid", "pairing_code_invalid"),
    ("--reply-pairing-code", "pairing_code"),
    ("--reply-pairing-link-unavailable", "pairing_link_unavailable"),
    ("--reply-pairing-unlink-unavailable", "pairing_unlink_unavailable"),
    ("--reply-rejected-unsupported-type", "rejected_unsupported_type"),
    ("--reply-rejected-too-large", "rejected_too_large"),
    ("--reply-rejected-could-not-receive", "rejected_could_not_receive"),
]


def test_conversations_create_has_a_flag_for_every_reply_text() -> None:
    assert sorted(member for _, member in _REPLY_FLAGS) == sorted(ReplyTexts.model_fields)


@pytest.mark.parametrize(("flag", "member"), _REPLY_FLAGS)
def test_conversations_create_sets_one_reply_text(monkeypatch: pytest.MonkeyPatch, flag: str, member: str) -> None:
    text = "Lo sentimos, algo salió mal. Inténtalo de nuevo."

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["reply_texts"] == {member: text}
        return data_response({"created": True})

    result = run_cli(monkeypatch, handler, [*_CHANNEL_ROUTE_ARGS, flag, text])
    assert result.exit_code == 0, result.output


def test_conversations_create_packs_every_reply_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["reply_texts"] == {member: f"text for {member}" for _, member in _REPLY_FLAGS}
        return data_response({"created": True})

    args = [part for flag, member in _REPLY_FLAGS for part in (flag, f"text for {member}")]
    result = run_cli(monkeypatch, handler, [*_CHANNEL_ROUTE_ARGS, *args])
    assert result.exit_code == 0, result.output


def test_conversations_create_omits_reply_texts_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "reply_texts" not in body
        return data_response({"created": True})

    result = run_cli(monkeypatch, handler, _CHANNEL_ROUTE_ARGS)
    assert result.exit_code == 0, result.output


def test_conversations_create_surfaces_a_refused_reply_text(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["reply_texts"] == {"error": " "}
        return error_response("reply_texts.error: must not be blank", 422)

    result = run_cli(monkeypatch, handler, [*_CHANNEL_ROUTE_ARGS, "--reply-error", " "])
    assert result.exit_code != 0
    assert "reply_texts.error: must not be blank" in visible(result.output)


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


def test_conversations_create_packs_full_overlap_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["overlap"] == {"running": "cancel", "deliver": "all", "settle_seconds": 5}
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
            "--overlap-running",
            "cancel",
            "--overlap-deliver",
            "all",
            "--overlap-settle-seconds",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output


def test_conversations_create_packs_only_given_overlap_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["overlap"] == {"deliver": "all"}
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
            "--overlap-deliver",
            "all",
        ],
    )
    assert result.exit_code == 0, result.output


def test_conversations_create_omits_overlap_when_no_knob_given(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert "overlap" not in body
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


def test_conversations_create_forwards_a_server_overlap_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cross-field rule and the bounds are the server's; a 400 surfaces loudly like any create error.
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response('settle_seconds > 0 requires deliver="all" or running="cancel"', 400)

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
            "--overlap-settle-seconds",
            "5",
        ],
    )
    assert result.exit_code != 0
    assert "settle_seconds" in visible(result.output)


def test_conversations_create_prints_the_account_refusal_and_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    refusal = (
        "execution key 'alice' is an account, not an api key; a route, hook, trigger link or schedule runs as an "
        "api key, so bind an api key instead"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["execution_key"] == "alice"
        return error_response(refusal, 400)

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
            "alice",
        ],
    )
    assert result.exit_code == 1
    assert result.output == f"Error (HTTP 400): {refusal}\n"


def test_conversations_create_help_lists_the_overlap_options(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - --help exits before any call
        raise AssertionError("--help must not reach the server")

    result = run_cli(monkeypatch, handler, ["conversations", "create", "--help"])
    assert result.exit_code == 0, result.output
    text = visible(result.output)
    assert "--overlap-running" in text
    assert "--overlap-deliver" in text
    assert "--overlap-settle-seconds" in text


def test_conversations_create_help_says_the_error_reply_yields_to_the_targets_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - --help exits before any call
        raise AssertionError("--help must not reach the server")

    result = run_cli(monkeypatch, handler, ["conversations", "create", "--help"])
    assert result.exit_code == 0, result.output
    assert (
        "Reply sent when a turn fails and the target gave no reply of its own. Unset sends the built-in English text."
    ) in visible(result.output)


def test_conversations_list_renders_the_overlap_column(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversations"
        return data_response(
            {"items": [{"route_name": "chat", "overlap": {"running": "cancel", "deliver": "all", "settle_seconds": 5}}]}
        )

    result = run_cli(monkeypatch, handler, ["conversations", "list"])
    assert result.exit_code == 0, result.output
    assert "overlap" in result.output.splitlines()[0]
    assert '"running": "cancel"' in result.output


def test_conversations_failed_renders_the_successor_id_column(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/conversations/messages/failed"
        return data_response({"items": [{"message_id": "m1", "answer_status": "superseded", "successor_id": "m2"}]})

    result = run_cli(monkeypatch, handler, ["conversations", "failed"])
    assert result.exit_code == 0, result.output
    assert "successor_id" in result.output.splitlines()[0]
    assert "m2" in result.output


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


def test_conversations_delete_person_of_an_absorbed_id_surfaces_the_409_and_exits_non_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = (
        "conversation person 'p-old' was absorbed into person 'p-new' by a link and is not erased on its own: "
        "erase 'p-new', which clears 'p-new' and every person it absorbed, 'p-old' included"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/conversations/persons/p-old"
        return httpx.Response(409, json={"error": message, "absorbed_into_person_id": "p-new"})

    result = run_cli(monkeypatch, handler, ["conversations", "delete-person", "p-old"])

    assert result.exit_code != 0
    shown = visible(result.output)
    assert message in shown
    assert "409" in shown


def test_conversations_transcript_renders_send_records(monkeypatch: pytest.MonkeyPatch) -> None:
    sends = [{"part": 0, "chars": 5, "provider_message_ids": ["wamid.1"]}]

    def handler(request: httpx.Request) -> httpx.Response:
        return data_response(
            {
                "items": [
                    {"message_id": "s1", "origin": "send", "delivery_status": "outcome_unknown", "sends": []},
                    {"message_id": "s2", "origin": "send", "delivery_status": "sending", "sends": sends},
                ]
            }
        )

    result = run_cli(monkeypatch, handler, ["conversations", "transcript", "chat", "bridge:chat:+1"])

    assert result.exit_code == 0, result.output
    header, _rule, *rows = result.output.splitlines()
    assert {"origin", "delivery_status", "sends"} <= set(header.split())
    assert "outcome_unknown" in rows[0]
    assert "send" in rows[0].split()
    assert "sending" in rows[1]
    assert "wamid.1" in rows[1]


def test_conversations_get_message_renders_its_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return data_response(
            {
                "message_id": "s1",
                "origin": "send",
                "delivery_status": "provisional",
                "sends": [
                    {"part": 0, "chars": 5, "provider_message_ids": ["wamid.1"]},
                    {"part": 1, "chars": 7, "provider_message_ids": ["wamid.2", "wamid.3"]},
                ],
            }
        )

    result = run_cli(monkeypatch, handler, ["conversations", "get-message", "chat", "s1"])

    assert result.exit_code == 0, result.output
    rows = {line.split()[0]: line for line in result.output.splitlines()[2:]}
    assert rows["origin"].split() == ["origin", "send"]
    assert rows["delivery_status"].split() == ["delivery_status", "provisional"]
    assert all(wamid in rows["sends"] for wamid in ("wamid.1", "wamid.2", "wamid.3"))


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
