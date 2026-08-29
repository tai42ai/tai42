"""The inbound webhook door: verification, bounds, scoping, and forward policy."""

from __future__ import annotations

import importlib
import json
import sys
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tai42_contract.channels import AnswerForwardError, InboundAnswerOutcome
from tai42_contract.conversations import BlankInboundTextError
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.inbound import inbound
from tai42_channel_telegram.settings import TelegramSettings

from .conftest import make_inbound_request

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


async def test_valid_reply_invokes_shared_ladder_and_acks_forwarded(http_recorder, fake_redis, channels):
    # The reply is handed to the ONE shared ladder with the anchor id as the
    # correlation key, the reply text as the answer, and the bridge context; a
    # FORWARDED outcome acks "forwarded".
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))

    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(channels.inbound_calls) == 1
    call = channels.inbound_calls[0]
    assert call.channel_id == "telegram"
    assert call.correlation_key == "777:42"  # the replied-to anchor scoped by its chat
    assert call.answer == "the blue one"
    assert call.bridge.channel_id == "telegram"
    assert call.bridge.our_identity == "123456"  # the bot's numeric id
    assert call.bridge.client_address == "777"
    assert call.bridge.cap_key == "777"
    assert call.bridge.provider_message_id == "5"  # the update id
    assert call.bridge.bridge_text == "the blue one"
    # The plugin no longer forwards itself; the ladder owns that.
    assert _forward_requests(http_recorder) == []


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


async def test_reply_from_allowlisted_chat_reaches_the_ladder(http_recorder, fake_redis, channels):
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    response = await inbound(make_inbound_request(_reply_update(chat_id=888), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(channels.inbound_calls) == 1


async def test_reply_from_chat_allowlisted_only_by_username_reaches_the_ladder(
    http_recorder, fake_redis, channels, monkeypatch: pytest.MonkeyPatch
):
    # The allowlist names the chat by @username alone; the update's numeric
    # chat id appears nowhere in the configuration, yet the reply matches on
    # "@" + chat.username and reaches the ladder.
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", "@ops_bot")
    reset_all_settings()
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    update = _reply_update(chat_id=424242, username="ops_bot")
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(channels.inbound_calls) == 1
    assert channels.inbound_calls[0].answer == "the blue one"
    assert channels.inbound_calls[0].bridge.client_address == "424242"


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


async def test_typing_action_failure_is_logged_and_webhook_survives(http_recorder, fake_redis, conversations, caplog):
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


async def test_pending_question_reply_resolves_ask_not_bridge(http_recorder, fake_redis, conversations, channels):
    # a ForceReply reply matching a pending question resolves the ask via the ladder
    # and never reaches the caller's fresh-turn bridge.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(channels.inbound_calls) == 1
    assert conversations.accept_calls == []  # the caller never bridges a resolved answer


async def test_expired_force_reply_falls_through_to_bridge(http_recorder, fake_redis, conversations, channels):
    # A ForceReply reply from a recipient chat whose question expired is a correlation
    # miss: the ladder returns NO_CORRELATION and the CALLER bridges it as a fresh turn
    # (never a silent ignore).
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert len(channels.inbound_calls) == 1  # the ladder was consulted first
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


async def test_ladder_bridged_outcome_acks_accepted(http_recorder, fake_redis, channels):
    # The ladder's BRIDGED outcome (the ask is gone / a hard mismatch — it already
    # bridged the reply internally) acks "accepted", the same wire a fresh-turn bridge
    # returns. The plugin passes the update id as the bridge's dedupe key.
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert len(channels.inbound_calls) == 1
    assert channels.inbound_calls[0].bridge.provider_message_id == "5"  # the update id — dedupes a redelivery


async def test_ladder_forward_error_propagates_so_telegram_redelivers(http_recorder, fake_redis, channels):
    # A 401/413/5xx / transport fault surfaces as AnswerForwardError from the ladder;
    # the plugin lets it propagate (-> 500) so Telegram redelivers and re-runs the
    # ladder — the answer is never silently lost.
    channels.inbound_error = AnswerForwardError("interactions answer door rejected the answer: HTTP 500")
    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))


async def test_ladder_retry_kept_outcome_acks_rejected(http_recorder, fake_redis, channels):
    # The ladder's RETRY_KEPT outcome (the door rejected a re-answerable ask; the
    # correlation is kept and the guest was told what's expected) acks "rejected".
    channels.inbound_outcome = InboundAnswerOutcome.RETRY_KEPT
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "rejected"}}
    assert len(channels.inbound_calls) == 1


# --- inline-keyboard callback taps (select / suggested-reply / notify options) ---


def _callback_update(
    chat_id: int = 777,
    message_id: int = 42,
    data: Any = "1",
    update_id: int = 9,
    username: str | None = None,
) -> dict[str, Any]:
    """A Telegram update carrying an inline-keyboard button tap (callback_query)."""
    chat: dict[str, Any] = {"id": chat_id}
    if username is not None:
        chat["username"] = username
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": chat_id},
            "message": {"message_id": message_id, "chat": chat},
            "data": data,
        },
    }


def _seed_options(fake_redis: Any, options: list[str], message_id: int = 42, chat_id: int = 777) -> None:
    # The option side record is chat-scoped, exactly as the reader (get_options) looks
    # it up: {chat_id}:{message_id} (a Telegram message_id is unique only per chat).
    fake_redis.data[f"channel:telegram:opts:{chat_id}:{message_id}"] = json.dumps(options)


def _answered_callbacks(recorder: Any) -> list[httpx.Request]:
    return [r for r in recorder.requests if str(r.url).endswith("/answerCallbackQuery")]


async def test_select_tap_maps_index_to_text_and_resolves_via_ladder(http_recorder, fake_redis, channels):
    # A tap on a select button (callback_data = the index) maps back to the exact
    # option text via the side record and resolves through the ladder with the anchor
    # message id as the correlation key; the callback query is answered (spinner clears).
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    _seed_options(fake_redis, ["red", "blue"])
    response = await inbound(make_inbound_request(_callback_update(data="1"), headers=_VALID_HEADERS))

    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(channels.inbound_calls) == 1
    call = channels.inbound_calls[0]
    assert call.correlation_key == "777:42"  # the anchor scoped by its chat
    assert call.answer == "blue"  # options[1]
    assert call.bridge.provider_message_id == "9"  # the update id
    assert len(_answered_callbacks(http_recorder)) == 1


async def test_notify_option_tap_bridges_on_correlation_miss(http_recorder, fake_redis, channels, conversations):
    # A notify-option tap has no pending ask: the ladder returns NO_CORRELATION and the
    # option text enters the conversation as a visitor message (a bridged turn).
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION
    _seed_options(fake_redis, ["a", "b", "c"])
    response = await inbound(make_inbound_request(_callback_update(data="2"), headers=_VALID_HEADERS))

    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert len(channels.inbound_calls) == 1  # the ladder was consulted first
    assert len(conversations.accept_calls) == 1
    assert conversations.accept_calls[0].text == "c"  # options[2]
    assert conversations.accept_calls[0].client_address == "777"


async def test_tap_from_non_recipient_chat_bridges_without_ladder(http_recorder, fake_redis, channels, conversations):
    # A tap from a chat that is not a configured recipient never reaches the answer
    # ladder — the option text bridges directly (the same gate the typed-reply path uses).
    # The side record is scoped to that tap's own chat (111).
    _seed_options(fake_redis, ["x", "y"], chat_id=111)
    response = await inbound(make_inbound_request(_callback_update(chat_id=111, data="0"), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert channels.inbound_calls == []
    assert len(conversations.accept_calls) == 1
    assert conversations.accept_calls[0].text == "x"


async def test_tap_with_no_option_record_is_acked_ignored(http_recorder, fake_redis, channels, conversations):
    # A tap on a stale keyboard (the ask expired, the side record is gone) is acked and
    # ignored — never a ladder call or a bridge, and never a 5xx redelivery loop.
    response = await inbound(make_inbound_request(_callback_update(data="0"), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert channels.inbound_calls == []
    assert conversations.accept_calls == []
    # The callback query is still answered so the button spinner clears.
    assert len(_answered_callbacks(http_recorder)) == 1


@pytest.mark.parametrize("data", ["5", "-1", "abc", "", "²"])
async def test_tap_with_bad_index_is_acked_ignored(http_recorder, fake_redis, channels, data: str):
    # An out-of-range, negative, non-ASCII-digit or non-numeric callback_data is not an
    # answer: acked and ignored, never a poison-tap redelivery loop.
    _seed_options(fake_redis, ["only-one"])
    response = await inbound(make_inbound_request(_callback_update(data=data), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert channels.inbound_calls == []


async def test_callback_answer_failure_does_not_fail_webhook(http_recorder, fake_redis, channels, caplog):
    # answerCallbackQuery failing (a non-200) is logged and swallowed — the tap still
    # resolves and the webhook still acks (never a 5xx that redelivers the whole update).
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    _seed_options(fake_redis, ["red", "blue"])
    http_recorder.responder = lambda request: (
        httpx.Response(500)
        if str(request.url).endswith("/answerCallbackQuery")
        else httpx.Response(200, json={"ok": True})
    )
    with caplog.at_level("WARNING"):
        response = await inbound(make_inbound_request(_callback_update(data="0"), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert any("answerCallbackQuery" in record.message for record in caplog.records)


# --- cross-chat isolation: a Telegram message_id is unique only PER CHAT ---


async def test_typed_reply_correlation_key_is_scoped_by_the_replying_chat(http_recorder, fake_redis, channels):
    # Two recipient chats (888 and 999) can each hold a pending ask anchored on the SAME
    # message_id 42. A typed reply from chat 888 is handed to the ladder under ITS OWN
    # scoped key 888:42 — never 999:42 — so it can only ever resolve chat 888's ask. The
    # store keys on the full string (channel:telegram:corr:888:42), so chat 999's ask,
    # stored under 999:42, is untouched.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    await inbound(make_inbound_request(_reply_update(chat_id=888, replied_message_id=42), headers=_VALID_HEADERS))
    assert len(channels.inbound_calls) == 1
    assert channels.inbound_calls[0].correlation_key == "888:42"
    assert channels.inbound_calls[0].correlation_key != "999:42"


async def test_callback_tap_correlation_key_is_scoped_by_the_tapping_chat(http_recorder, fake_redis, channels):
    # The same isolation on the tap path: a button tap from chat 888 on an anchor whose
    # message_id 42 is shared with another chat resolves under 888:42, never a bare 42 that
    # a same-id anchor in chat 999 would also match.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    _seed_options(fake_redis, ["red", "blue"], message_id=42, chat_id=888)
    await inbound(make_inbound_request(_callback_update(chat_id=888, message_id=42, data="1"), headers=_VALID_HEADERS))
    assert len(channels.inbound_calls) == 1
    assert channels.inbound_calls[0].correlation_key == "888:42"
    assert channels.inbound_calls[0].answer == "blue"


async def test_cross_chat_tap_finds_no_option_record_and_does_not_resolve(http_recorder, fake_redis, channels):
    # Chat 999 delivered an options message anchored on message_id 42 (its side record is
    # opts:999:42). A tap arriving from chat 888 carrying the same message_id 42 looks up
    # opts:888:42 — a MISS — so it is acked-ignored and never resolves chat 999's ask.
    _seed_options(fake_redis, ["red", "blue"], message_id=42, chat_id=999)
    response = await inbound(
        make_inbound_request(_callback_update(chat_id=888, message_id=42, data="1"), headers=_VALID_HEADERS)
    )
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert channels.inbound_calls == []
    # Chat 999's own side record is untouched.
    assert fake_redis.data["channel:telegram:opts:999:42"]


# --- _resolve_answer guards on the ForceReply / recipient path ---


async def test_recipient_reply_without_update_id_is_rejected(http_recorder, fake_redis, channels):
    # A ForceReply reply from a recipient chat reaches _resolve_answer, but update_id is
    # the bridge's idempotency key: a reply lacking it is malformed (400), never resolved
    # or bridged without one.
    update = {
        "message": {
            "message_id": 1001,
            "chat": {"id": 777},
            "reply_to_message": {"message_id": 42},
            "text": "the blue one",
        }
    }
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 400
    assert _body(response) == {"error": "update carries no integer update_id"}
    assert channels.inbound_calls == []


async def test_recipient_reply_with_malformed_bot_token_misconfigures(
    http_recorder, fake_redis, channels, monkeypatch: pytest.MonkeyPatch
):
    # Resolving a recipient-chat answer derives this bot's numeric id from the token; a
    # token with no numeric prefix is a loud 500 (channel misconfigured), never a silent
    # resolve. The typing action still fires (the token is non-empty, just malformed).
    monkeypatch.setenv("CHANNEL_TELEGRAM_BOT_TOKEN", "no-colon-token")
    reset_all_settings()
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 500
    assert _body(response) == {"error": "channel misconfigured"}
    assert channels.inbound_calls == []


# --- _resolve_callback defensive guards ---


async def test_resolve_callback_non_dict_query_is_ignored(http_recorder, fake_redis):
    # The defensive guard for a non-dict callback_query (the inbound() caller checks this,
    # so it is only reachable by a direct call): acked-ignored, never a raise.
    from tai42_channel_telegram.inbound import _resolve_callback
    from tai42_channel_telegram.settings import telegram_settings

    response = await _resolve_callback(telegram_settings(), {"callback_query": "not-a-dict"})
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}


async def test_callback_tap_without_anchor_message_id_is_ignored(http_recorder, fake_redis, channels):
    # A callback_query whose message carries no message_id has no anchor to key the option
    # record on: acked-ignored (the query is still answered so the spinner clears), never a
    # ladder call or a 5xx redelivery loop.
    update = {
        "update_id": 9,
        "callback_query": {"id": "cb-1", "message": {"chat": {"id": 777}}, "data": "0"},
    }
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert channels.inbound_calls == []
    assert len(_answered_callbacks(http_recorder)) == 1
