"""The inbound webhook door: verification, bounds, scoping, and forward policy."""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tai42_contract.conversations import BlankInboundTextError
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.inbound import inbound
from tai42_channel_telegram.settings import TelegramSettings
from tests.conftest import make_inbound_request

_CALLBACK = "https://example.test/api/interactions/callback/tkt"
_VALID_HEADERS = {"X-Telegram-Bot-Api-Secret-Token": "s3cret_token"}


def _reply_update(
    chat_id: int = 777,
    replied_message_id: Any = 42,
    text: Any = "the blue one",
    username: str | None = None,
) -> dict[str, Any]:
    """A Telegram update carrying a ForceReply answer to a delivered question."""
    chat: dict[str, Any] = {"id": chat_id}
    if username is not None:
        chat["username"] = username
    message: dict[str, Any] = {
        "message_id": 1001,
        "chat": chat,
        "reply_to_message": {"message_id": replied_message_id},
    }
    if text is not None:
        message["text"] = text
    return {"update_id": 5, "message": message}


def _body(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def _forward_requests(recorder: Any) -> list[httpx.Request]:
    """Recorded outbound requests minus the inbound ``sendChatAction`` typing
    signal, which fires for every processable message ahead of the ask/bridge split."""
    return [r for r in recorder.requests if not str(r.url).endswith("/sendChatAction")]


def _typing_requests(recorder: Any) -> list[httpx.Request]:
    return [r for r in recorder.requests if str(r.url).endswith("/sendChatAction")]


def test_route_metadata(stub_app):
    sys.modules.pop("tai42_channel_telegram.inbound", None)
    importlib.import_module("tai42_channel_telegram.inbound")
    routes = [r for r in stub_app.http.routes if r.path == "/inbound"]
    assert routes
    route = routes[-1]
    assert route.methods == ["POST"]
    assert route.authed is None
    assert route.tags == ["channels"]
    assert route.summary == "Telegram channel inbound webhook"


async def test_valid_reply_forwards_answer_and_clears_mapping(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))

    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    forwards = _forward_requests(http_recorder)
    assert len(forwards) == 1
    forward = forwards[0]
    assert str(forward.url) == _CALLBACK
    assert json.loads(forward.content) == {"answer": "the blue one"}
    assert fake_redis.data == {}


async def test_missing_wrong_and_wrong_length_secret_all_deny_identically(http_recorder, fake_redis):
    responses = [
        await inbound(make_inbound_request(_reply_update())),
        await inbound(make_inbound_request(_reply_update(), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-tok"})),
        # A different LENGTH must not short-circuit the compare: both sides are
        # sha256-hashed to 32 bytes before compare_digest.
        await inbound(
            make_inbound_request(_reply_update(), headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret_token_longer"})
        ),
    ]
    assert [r.status_code for r in responses] == [401, 401, 401]
    assert len({bytes(r.body) for r in responses}) == 1
    assert http_recorder.requests == []


async def test_empty_env_secret_fails_closed(http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CHANNEL_TELEGRAM_WEBHOOK_SECRET", "")
    reset_all_settings()
    response = await inbound(
        make_inbound_request(_reply_update(), headers={"X-Telegram-Bot-Api-Secret-Token": ""}),
    )
    assert response.status_code == 500
    assert _body(response) == {"error": "channel misconfigured"}
    assert http_recorder.requests == []


async def test_empty_configured_secret_never_verifies_even_matching(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # A set-but-empty SecretStr (constructed directly — the env layer drops
    # empty vars) must fail CLOSED even when the header matches it byte-for-byte.
    settings = TelegramSettings(webhook_secret=SecretStr(""))
    # Patch the handler's own globals: `inbound` here is the function object,
    # and a re-imported module elsewhere must not divert the patch target.
    monkeypatch.setitem(inbound.__globals__, "telegram_settings", lambda: settings)
    response = await inbound(
        make_inbound_request(_reply_update(), headers={"X-Telegram-Bot-Api-Secret-Token": ""}),
    )
    assert response.status_code == 500
    assert http_recorder.requests == []


async def test_unset_secret_fails_closed(http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CHANNEL_TELEGRAM_WEBHOOK_SECRET")
    reset_all_settings()
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 500
    assert _body(response) == {"error": "channel misconfigured"}


def _text_update(chat_id: int = 777, text: str = "hello bridge", update_id: int = 7, username: str | None = None):
    """A plain (non-reply) text update — a bridge message, not an answer."""
    chat: dict[str, Any] = {"id": chat_id}
    if username is not None:
        chat["username"] = username
    return {"update_id": update_id, "message": {"message_id": 1001, "chat": chat, "text": text}}


async def test_bridge_only_deployment_no_recipients_does_not_misconfigure(
    http_recorder, fake_redis, conversations, monkeypatch: pytest.MonkeyPatch
):
    # A bridge-only deployment sets no recipient allowlist/default; a client message
    # still reaches the bridge instead of 500-ing on missing recipient config.
    monkeypatch.delenv("CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")
    monkeypatch.delenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS")
    reset_all_settings()
    response = await inbound(make_inbound_request(_text_update(chat_id=555), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert len(conversations.accept_calls) == 1
    assert conversations.accept_calls[0].client_address == "555"


async def test_reply_from_allowlisted_chat_forwards(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    response = await inbound(make_inbound_request(_reply_update(chat_id=888), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(_forward_requests(http_recorder)) == 1


async def test_reply_from_chat_allowlisted_only_by_username_forwards(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # The allowlist names the chat by @username alone; the update's numeric
    # chat id appears nowhere in the configuration, yet the reply matches on
    # "@" + chat.username and is forwarded.
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", "@ops_bot")
    reset_all_settings()
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    update = _reply_update(chat_id=424242, username="ops_bot")
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    forwards = _forward_requests(http_recorder)
    assert len(forwards) == 1
    assert json.loads(forwards[0].content) == {"answer": "the blue one"}
    assert fake_redis.data == {}


async def test_oversized_body_413_bounded_while_streaming(http_recorder, fake_redis):
    request = make_inbound_request(
        headers=_VALID_HEADERS,
        chunks=[b"x" * 600_000, b"x" * 600_000, b"tail"],
    )
    response = await inbound(request)
    assert response.status_code == 413
    assert _body(response) == {"error": "payload too large"}
    # The cap fired mid-stream: the trailing chunk was never pulled.
    assert request.scope["_pending_body_messages"]
    assert http_recorder.requests == []


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2, 3]"])
async def test_non_object_body_400(http_recorder, fake_redis, raw: bytes):
    response = await inbound(make_inbound_request(raw=raw, headers=_VALID_HEADERS))
    assert response.status_code == 400
    assert _body(response) == {"error": "body must be a JSON object"}
    assert http_recorder.requests == []


@pytest.mark.parametrize(
    "update",
    [
        {"update_id": 5},  # no message at all
        {"update_id": 5, "message": {"message_id": 1, "chat": "778", "text": "hi"}},  # chat is not an object
        {"update_id": 5, "message": {"message_id": 1, "chat": {"id": "abc"}, "text": "hi"}},  # chat id not numeric
        {"update_id": 5, "message": {"message_id": 1, "chat": {"id": 777}}},  # no text (nothing to bridge)
        _reply_update(text=None),  # reply without text (e.g. a photo)
    ],
)
async def test_out_of_scope_updates_are_acked_and_ignored(
    http_recorder, fake_redis, conversations, update: dict[str, Any]
):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert http_recorder.requests == []
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}
    assert conversations.accept_calls == []


async def test_uncorrelated_routed_message_reaches_bridge_with_verbatim_args(http_recorder, fake_redis, conversations):
    # A plain text message correlates to no question -> the bridge, with our_identity
    # = the bot's numeric id, client_address = the numeric chat id, id = the update id.
    response = await inbound(make_inbound_request(_text_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert _forward_requests(http_recorder) == []  # bridge makes no forward call
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.channel == "telegram"
    assert call.our_identity == "123456"
    assert call.client_address == "777"
    assert call.text == "hello bridge"
    assert call.provider_message_id == "7"


async def test_inbound_fires_typing_chat_action_before_bridge(http_recorder, fake_redis, conversations):
    # Every processable message shows a "working on it" typing action: a
    # sendChatAction POST carrying {chat_id, action: "typing"}, fired ahead of the
    # ask/bridge split; the message still bridges.
    response = await inbound(make_inbound_request(_text_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    typing = _typing_requests(http_recorder)
    assert len(typing) == 1
    assert typing[0].method == "POST"
    assert str(typing[0].url).endswith("/sendChatAction")
    assert json.loads(typing[0].content) == {"chat_id": 777, "action": "typing"}
    assert len(conversations.accept_calls) == 1  # bridge still reached


async def test_typing_action_failure_is_logged_and_webhook_survives(
    http_recorder, fake_redis, conversations, caplog
):
    # A non-200 on sendChatAction raises ChannelDeliveryError inside the client; the
    # door catches it, logs at WARNING, and the message still bridges (never a 5xx
    # that would make Telegram redeliver the whole update).
    http_recorder.responder = lambda request: (
        httpx.Response(500) if str(request.url).endswith("/sendChatAction") else httpx.Response(200, json={"ok": True})
    )
    with caplog.at_level("WARNING"):
        response = await inbound(make_inbound_request(_text_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert len(_typing_requests(http_recorder)) == 1  # the signal was attempted
    assert any("typing action" in record.message for record in caplog.records)
    assert len(conversations.accept_calls) == 1  # bridge still reached


async def test_uncorrelated_unrouted_message_is_acked_no_turn(http_recorder, fake_redis, conversations):
    # accept() raises when no route matches; the door acks (200) so Telegram stops
    # redelivering a permanently-unrouted address.
    conversations.accept_error = LookupError("no route")
    response = await inbound(make_inbound_request(_text_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert len(conversations.accept_calls) == 1


async def test_uncorrelated_blank_message_is_acked_no_turn(http_recorder, fake_redis, conversations, caplog):
    # A whitespace-only text passes the door's own text pre-filter, reaches the
    # bridge, and accept() raises BlankInboundTextError; the door acks (200) so
    # Telegram stops redelivering, never a 5xx retry-storm, and no turn is made.
    conversations.accept_error = BlankInboundTextError("inbound text is blank")
    with caplog.at_level("WARNING"):
        response = await inbound(make_inbound_request(_text_update(text="   "), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert len(conversations.accept_calls) == 1
    assert any("blank" in record.message for record in caplog.records)


async def test_pending_question_reply_resolves_ask_not_bridge(http_recorder, fake_redis, conversations):
    # a ForceReply reply matching a pending question resolves the ask
    # and never reaches the bridge.
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(_forward_requests(http_recorder)) == 1
    assert conversations.accept_calls == []


async def test_expired_force_reply_falls_through_to_bridge(http_recorder, fake_redis, conversations):
    # A ForceReply reply from a recipient chat whose question expired is a correlation
    # miss -> the bridge, NOT a silent ignore.
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert _forward_requests(http_recorder) == []  # bridge makes no forward call
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.client_address == "777"
    assert call.text == "the blue one"
    assert call.provider_message_id == "5"


async def test_bridge_client_address_is_numeric_chat_id_even_with_username(http_recorder, fake_redis, conversations):
    # A username on the chat never becomes the address — the numeric id does.
    response = await inbound(
        make_inbound_request(_text_update(chat_id=424242, username="ops_bot"), headers=_VALID_HEADERS)
    )
    assert response.status_code == 200
    assert len(conversations.accept_calls) == 1
    assert conversations.accept_calls[0].client_address == "424242"


async def test_bridge_cap_key_is_the_attested_chat_id(http_recorder, fake_redis, conversations):
    # A provider channel attests the address, so the accountable turn-cap key it passes
    # is that same attested chat id — no behavior change from keying on the address.
    response = await inbound(make_inbound_request(_text_update(chat_id=555), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.cap_key == "555"
    assert call.cap_key == call.client_address


async def test_signature_failure_short_circuits_before_bridge(http_recorder, fake_redis, conversations):
    # Transport auth is first on every path: a bridge-shaped message with a bad
    # secret denies (401) and never reaches accept().
    response = await inbound(
        make_inbound_request(_text_update(), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-tok"})
    )
    assert response.status_code == 401
    assert conversations.accept_calls == []


async def test_bridge_transient_failure_propagates(http_recorder, fake_redis, conversations):
    # A non-route failure (e.g. an unavailable dependency) propagates -> 500 so
    # Telegram redelivers rather than dropping the message.
    conversations.accept_error = RuntimeError("redis down")
    with pytest.raises(RuntimeError, match="redis down"):
        await inbound(make_inbound_request(_text_update(), headers=_VALID_HEADERS))


async def test_bridge_message_without_update_id_is_rejected(http_recorder, fake_redis, conversations):
    # update_id is the bridge's idempotency key; a message lacking it is malformed
    # (400), never bridged without one.
    update = {"message": {"message_id": 1001, "chat": {"id": 777}, "text": "hi"}}
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 400
    assert _body(response) == {"error": "update carries no integer update_id"}
    assert conversations.accept_calls == []


async def test_malformed_bot_token_bridge_misconfigures(http_recorder, fake_redis, conversations, monkeypatch):
    # our_identity is derived at the point of use; a token with no numeric prefix is
    # a loud 500, never a silent default.
    monkeypatch.setenv("CHANNEL_TELEGRAM_BOT_TOKEN", "no-colon-token")
    reset_all_settings()
    response = await inbound(make_inbound_request(_text_update(), headers=_VALID_HEADERS))
    assert response.status_code == 500
    assert _body(response) == {"error": "channel misconfigured"}
    assert conversations.accept_calls == []


async def test_callback_404_is_terminal_mapping_dropped(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    http_recorder.responder = lambda request: httpx.Response(404)
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "stale"}}
    assert fake_redis.data == {}


async def test_callback_400_keeps_mapping_for_a_retyped_answer(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    http_recorder.responder = lambda request: httpx.Response(400)
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "rejected"}}
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}


async def test_callback_500_raises_so_telegram_redelivers(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK
    http_recorder.responder = lambda request: httpx.Response(500)
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}


async def test_callback_transport_error_propagates(http_recorder, fake_redis):
    fake_redis.data["channel:telegram:corr:42"] = _CALLBACK

    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("door unreachable")

    http_recorder.responder = responder
    with pytest.raises(httpx.ConnectError, match="door unreachable"):
        await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}
