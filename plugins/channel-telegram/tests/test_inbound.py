"""The inbound webhook door: verification, bounds, scoping, and forward policy."""

from __future__ import annotations

import importlib
import json
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tai42_contract.channels import AnswerForwardError, ChannelDeliveryError, InboundAnswerOutcome
from tai42_contract.conversations import BlankInboundTextError, InboundRejectionReason
from tai42_contract.interactions import (
    IngestedMedia,
    MediaItem,
    MediaKind,
    MediaSourceReadError,
    MediaStoreUnavailableError,
    MediaTooLargeError,
    MediaTypeNotAllowedError,
)
from tai42_kit.net import MediaFetchError, UrlGuardError
from tai42_kit.settings import reset_all_settings

import tai42_channel_telegram.inbound_media as inbound_media_module
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
    # The plugin does not forward itself; the ladder owns that.
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


def _text_update(
    chat_id: int = 777,
    text: str = "hello bridge",
    update_id: int = 7,
    username: str | None = None,
    language_code: str | None = None,
):
    """A plain (non-reply) text update — a bridge message, not an answer."""
    chat: dict[str, Any] = {"id": chat_id}
    if username is not None:
        chat["username"] = username
    message: dict[str, Any] = {"message_id": 1001, "chat": chat, "text": text}
    if language_code is not None:
        message["from"] = {"id": chat_id, "language_code": language_code}
    return {"update_id": update_id, "message": message}


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
    assert call.locale is None  # no sender language_code on this update


async def test_bridge_maps_the_sender_language_code_to_the_turn_locale(http_recorder, fake_redis, conversations):
    response = await inbound(make_inbound_request(_text_update(language_code="pt-br"), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert len(conversations.accept_calls) == 1
    assert conversations.accept_calls[0].locale == "pt-BR"


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
    # is that same attested chat id — the cap key equals the client address.
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


async def test_ladder_bridged_kept_outcome_acks_accepted(http_recorder, fake_redis, channels):
    # The ladder's BRIDGED_KEPT outcome (a bridge-policy ask rejected the reply: the
    # correlation is KEPT and the reply was bridged as a digression turn) acks "accepted",
    # the same wire a released BRIDGE returns. Without the ack-map entry this reachable
    # outcome raised KeyError -> 500 -> provider redelivery loop.
    channels.inbound_outcome = InboundAnswerOutcome.BRIDGED_KEPT
    response = await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert len(channels.inbound_calls) == 1


async def test_ladder_forward_error_propagates_so_telegram_redelivers(http_recorder, fake_redis, channels):
    # A 401/413/5xx / transport fault surfaces as AnswerForwardError from the ladder;
    # the plugin lets it propagate (-> 500) so Telegram redelivers and re-runs the
    # ladder — the answer is never silently lost.
    channels.inbound_error = AnswerForwardError("interactions answer door rejected the answer: HTTP 500")
    with pytest.raises(AnswerForwardError, match="HTTP 500"):
        await inbound(make_inbound_request(_reply_update(), headers=_VALID_HEADERS))


async def test_ladder_retry_kept_outcome_acks_rejected(http_recorder, fake_redis, channels):
    # The ladder's RETRY_KEPT outcome (the door rejected a re-answerable ask; the
    # correlation is kept and the participant was told what's expected) acks "rejected".
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
    # it up: {chat_id}:{message_id} (a Telegram message_id is unique only per chat). Plain
    # texts seed index-keyed records with no author-set id/description (the select /
    # suggested-reply ask shape); the wire token is the index.
    records = [
        {"callback_data": str(index), "text": text, "id": None, "description": None}
        for index, text in enumerate(options)
    ]
    fake_redis.data[f"channel:telegram:opts:{chat_id}:{message_id}"] = json.dumps(records)


def _seed_option_records(fake_redis: Any, records: list[dict], message_id: int = 42, chat_id: int = 777) -> None:
    # Seed the side record with explicit StoredOption dumps (to exercise author-set ids /
    # descriptions and their carry-back as params on a bridged tap).
    fake_redis.data[f"channel:telegram:opts:{chat_id}:{message_id}"] = json.dumps(records)


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


async def test_authored_id_tap_bridges_with_reply_id_and_description_params(
    http_recorder, fake_redis, channels, conversations
):
    # A tap whose wire token is an author-set id resolves to its option text, and on the
    # BRIDGE path (a notify option, no pending ask) carries the author-set id and the row's
    # description back as params.reply_id / params.reply_description.
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION
    _seed_option_records(
        fake_redis,
        [{"callback_data": "yes-1", "text": "Yes", "id": "yes-1", "description": "the affirmative"}],
    )
    response = await inbound(make_inbound_request(_callback_update(data="yes-1"), headers=_VALID_HEADERS))

    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.text == "Yes"  # resolved by the authored-id token, not an index
    assert call.params == {"reply_id": "yes-1", "reply_description": "the affirmative"}


async def test_answering_tap_forwards_reply_id_on_the_bridge_seam(http_recorder, fake_redis, channels):
    # A tap that ANSWERS a pending ask (recipient chat, FORWARDED) still hands the ladder the
    # author-set id via the InboundBridge params seam (the ladder forwards it to the callback
    # door alongside the answer); the answer text is the resolved option text.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    _seed_option_records(fake_redis, [{"callback_data": "yes-1", "text": "Yes", "id": "yes-1", "description": None}])
    response = await inbound(make_inbound_request(_callback_update(data="yes-1"), headers=_VALID_HEADERS))

    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(channels.inbound_calls) == 1
    call = channels.inbound_calls[0]
    assert call.answer == "Yes"
    assert call.bridge.params == {"reply_id": "yes-1"}


async def test_minted_option_tap_bridges_without_params(http_recorder, fake_redis, channels, conversations):
    # A tap on an option with no author-set id (a select/suggested-reply ask, or a plain
    # notify option) carries NO params on the bridge — there is nothing to echo back.
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION
    _seed_options(fake_redis, ["a", "b"])
    await inbound(make_inbound_request(_callback_update(data="1"), headers=_VALID_HEADERS))

    assert len(conversations.accept_calls) == 1
    assert conversations.accept_calls[0].text == "b"
    assert conversations.accept_calls[0].params is None


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


def _media_update(
    chat_id: int = 777,
    update_id: int = 7,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
    **members: Any,
) -> dict[str, Any]:
    """A message carrying one media member; a ``reply_to_message_id`` makes it a ForceReply reply."""
    message: dict[str, Any] = {"message_id": 1001, "chat": {"id": chat_id}, **members}
    if caption is not None:
        message["caption"] = caption
    if reply_to_message_id is not None:
        message["reply_to_message"] = {"message_id": reply_to_message_id}
    return {"update_id": update_id, "message": message}


# A well-formed served-media id: 43 urlsafe-base64 chars, matching the contract's route id.
_SERVED_ID = "A" * 43
# The bot token the test env injects (conftest ``channel_env``) — asserted absent from faults.
_BOT_TOKEN = "123456:test-token"


def _ingested(
    kind: MediaKind,
    *,
    media_id: str = _SERVED_ID,
    size: int = 900,
    sha256: str = "sha-hex",
    mime: str = "image/png",
    filename: str | None = None,
) -> IngestedMedia:
    """A served :class:`IngestedMedia` the media-seam stub returns for the happy path."""
    item = MediaItem(kind=kind, url=f"/api/interactions/media/{media_id}", filename=filename)
    return IngestedMedia(item=item, media_id=media_id, size=size, sha256=sha256, mime=mime)


def _fake_open_stream(
    *,
    chunks: tuple[bytes, ...] = (b"bytes",),
    content_type: str | None = None,
    content_length: int | None = None,
    raise_at_open: BaseException | None = None,
):
    """A stand-in for the kit's ``open_media_stream`` async context manager.

    Yields a ``MediaStream``-shaped object (``content_type``/``content_length``/``host``/
    ``chunks``); ``raise_at_open`` makes entering the context raise (a fetch-open fault)."""

    @asynccontextmanager
    async def _open(url: str, *, headers: Any = None, auth: Any = None, follow_redirects: bool = False):
        if raise_at_open is not None:
            raise raise_at_open

        async def _chunks():
            for chunk in chunks:
                yield chunk

        yield SimpleNamespace(
            content_type=content_type, content_length=content_length, host="file.telegram.test", chunks=_chunks()
        )

    return _open


def _getfile_responder(*, file_path: str = "photos/file_1.jpg", status: int = 200, ok: bool = True):
    """An http_recorder responder: ``getFile`` answers per ``status``/``ok``/``file_path``; every
    other call (the typing ``sendChatAction``) gets a generic ok:true result."""

    def responder(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/getFile"):
            if status != 200:
                return httpx.Response(status, json={"ok": False, "error_code": status, "description": "nope"})
            if not ok:
                return httpx.Response(200, json={"ok": False, "error_code": 400, "description": "nope"})
            return httpx.Response(200, json={"ok": True, "result": {"file_path": file_path}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": 777}}})

    return responder


async def test_captioned_photo_fetches_ingests_and_bridges_served_attachment(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # A captioned photo: getFile -> stream -> ingest_media -> a served attachment. The caption is
    # the turn text, media_id is the SERVED id (never the raw file_id), media_size is the actual
    # bytes read, and the typed MediaItem rides ``attachments``.
    http_recorder.responder = _getfile_responder()
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(content_type="image/png"))
    media.ingest_result = _ingested(MediaKind.IMAGE, size=900, mime="image/png")
    update = _media_update(
        photo=[{"file_id": "small", "file_size": 100}, {"file_id": "big", "file_size": 900}],
        caption="look at this",
    )
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.text == "look at this"
    assert call.params["media_kind"] == "image"
    assert call.params["media_id"] == _SERVED_ID
    assert call.params["media_sha256"] == media.ingest_result.sha256  # the seam's digest of the served bytes
    assert call.params["media_size"] == "900"
    assert call.attachments == [media.ingest_result.item]
    # The largest photo size's file_id was the one fetched; the vendor gives photos no mime, so the
    # seam's declared_mime falls back to the stream's content type.
    ingest = media.ingest_calls[0]
    assert ingest.declared_mime == "image/png"
    assert ingest.kind_hint == "image"
    assert ingest.origin.channel_id == "telegram"
    assert ingest.origin.participant_identity == "777"
    assert ingest.origin.message_id == "7"


async def test_captionless_document_bridges_served_attachment_and_sanitised_placeholder(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # A caption-less document: the "[document: <name>]" placeholder is the non-blank turn text and
    # the served id/mime/filename ride the parity params; media_id is the SERVED id.
    http_recorder.responder = _getfile_responder(file_path="documents/file_1.pdf")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream())
    media.ingest_result = _ingested(MediaKind.DOCUMENT, mime="application/pdf", filename="report.pdf")
    update = _media_update(
        document={"file_id": "doc1", "file_name": "report.pdf", "mime_type": "application/pdf", "file_size": 2048},
    )
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.text == "[document: report.pdf]"
    assert call.params["media_kind"] == "document"
    assert call.params["media_id"] == _SERVED_ID
    assert call.params["media_sha256"] == media.ingest_result.sha256  # the seam's digest of the served bytes
    assert call.params["media_filename"] == "report.pdf"
    assert call.params["media_mime_type"] == "application/pdf"
    assert call.attachments == [media.ingest_result.item]
    # The seam is handed the vendor-declared mime and the RAW filename to sanitise.
    assert media.ingest_calls[0].declared_mime == "application/pdf"
    assert media.ingest_calls[0].filename == "report.pdf"
    assert media.ingest_calls[0].declared_size == 2048


async def test_animation_with_document_member_bridges_as_animation_video(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # Telegram sends an animation as BOTH an animation and a document member; _MEDIA_SPECS orders
    # animation first, so the one bridged turn maps to the animation (generic video) kind and the
    # animation member's file_id is the one fetched, never the document member's.
    http_recorder.responder = _getfile_responder(file_path="animations/file_1.mp4")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(content_type="video/mp4"))
    media.ingest_result = _ingested(MediaKind.VIDEO, mime="video/mp4")
    update = _media_update(
        animation={"file_id": "anim1", "file_name": "clip.gif", "mime_type": "video/mp4", "file_size": 4096},
        document={"file_id": "doc1", "file_name": "clip.gif", "mime_type": "video/mp4", "file_size": 4096},
    )
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.params["media_kind"] == "video"
    assert call.params["media_kind"] != "document"
    assert call.params["media_id"] == _SERVED_ID


async def test_voice_note_bridges_as_voice_audio(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # A voice note maps to the generic audio kind with media_voice=true and the "[voice message]"
    # placeholder as its turn text; media_id is the served reference.
    http_recorder.responder = _getfile_responder(file_path="voice/file_1.ogg")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(content_type="audio/ogg"))
    media.ingest_result = _ingested(MediaKind.AUDIO, mime="audio/ogg")
    update = _media_update(voice={"file_id": "v1", "mime_type": "audio/ogg", "file_size": 512})
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.text == "[voice message]"
    assert call.params["media_kind"] == "audio"
    assert call.params["media_voice"] == "true"
    assert call.params["media_id"] == _SERVED_ID
    assert media.ingest_calls[0].declared_mime == "audio/ogg"


async def test_animated_sticker_bridges_with_animated_flag(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # An animated sticker carries sticker_animated=true and media_kind "sticker"; the served
    # attachment resolves to an image/video item. Its turn text is the "[sticker]" placeholder.
    http_recorder.responder = _getfile_responder(file_path="stickers/file_1.webp")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(content_type="image/webp"))
    media.ingest_result = _ingested(MediaKind.IMAGE, mime="image/webp")
    update = _media_update(sticker={"file_id": "s1", "is_animated": True, "file_size": 64})
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.text == "[sticker]"
    assert call.params["media_kind"] == "sticker"
    assert call.params["sticker_animated"] == "true"
    assert call.params["media_id"] == _SERVED_ID


async def test_media_bridge_dedupe_key_is_the_update_id(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # The bridged media turn's provider_message_id and the ingest origin.message_id are BOTH the
    # update id — the idempotency key a Telegram redelivery is deduped by.
    http_recorder.responder = _getfile_responder()
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(content_type="image/png"))
    media.ingest_result = _ingested(MediaKind.IMAGE)
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}], update_id=4242)
    await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert conversations.accept_calls[0].provider_message_id == "4242"
    assert media.ingest_calls[0].origin.message_id == "4242"


async def test_getfile_permanent_failure_rejects_could_not_receive(
    http_recorder, fake_redis, conversations, monkeypatch: pytest.MonkeyPatch
):
    # A getFile error response for an unknown/expired file is PERMANENT: notify COULD_NOT_RECEIVE
    # and ack (no caption -> notice + ack only), never a raise/redelivery and never a bridged turn.
    http_recorder.responder = _getfile_responder(status=404)
    update = _media_update(photo=[{"file_id": "gone", "file_size": 900}])
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert conversations.accept_calls == []
    assert len(conversations.rejected_calls) == 1
    rejected = conversations.rejected_calls[0]
    assert rejected.kind == "image"
    assert rejected.reason is InboundRejectionReason.COULD_NOT_RECEIVE


async def test_getfile_transient_5xx_raises_for_redelivery(
    http_recorder, fake_redis, conversations, monkeypatch: pytest.MonkeyPatch
):
    # A getFile 5xx is TRANSIENT: the handler raises (-> 500) so Telegram redelivers; no turn, no
    # rejection notice, no dedupe record.
    http_recorder.responder = _getfile_responder(status=503)
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    with pytest.raises(ChannelDeliveryError):
        await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert conversations.accept_calls == []
    assert conversations.rejected_calls == []


async def test_getfile_429_raises_for_redelivery(
    http_recorder, fake_redis, conversations, monkeypatch: pytest.MonkeyPatch
):
    # A getFile 429 (Too Many Requests — a vendor throttle) is TRANSIENT: the handler raises
    # (-> 500) so Telegram redelivers; the update is neither acked nor deduped, no turn, no notice.
    http_recorder.responder = _getfile_responder(status=429)
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    with pytest.raises(ChannelDeliveryError):
        await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert conversations.accept_calls == []
    assert conversations.rejected_calls == []


async def test_getfile_ok_false_body_rejects_could_not_receive(
    http_recorder, fake_redis, conversations, monkeypatch: pytest.MonkeyPatch
):
    # A 200 getFile body carrying ok:false (an unknown/expired file) is a PERMANENT
    # TelegramFilePermanentError: the door notifies COULD_NOT_RECEIVE and acks, never a raise.
    http_recorder.responder = _getfile_responder(ok=False)
    update = _media_update(photo=[{"file_id": "gone", "file_size": 900}])
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert conversations.accept_calls == []
    assert len(conversations.rejected_calls) == 1
    assert conversations.rejected_calls[0].kind == "image"
    assert conversations.rejected_calls[0].reason is InboundRejectionReason.COULD_NOT_RECEIVE


async def test_getfile_missing_file_path_rejects_could_not_receive(
    http_recorder, fake_redis, conversations, monkeypatch: pytest.MonkeyPatch
):
    # A 200 getFile body that is ok:true but carries no file_path leaves nothing to fetch: a PERMANENT
    # TelegramFilePermanentError -> the door notifies COULD_NOT_RECEIVE and acks, never a raise.
    def responder(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": 777}}})

    http_recorder.responder = responder
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert conversations.accept_calls == []
    assert len(conversations.rejected_calls) == 1
    assert conversations.rejected_calls[0].reason is InboundRejectionReason.COULD_NOT_RECEIVE


async def test_over_cap_media_rejects_too_large(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # The ingest seam raising MediaTooLargeError is a PERMANENT reject mapped to TOO_LARGE + ack.
    http_recorder.responder = _getfile_responder(file_path="documents/file_1.pdf")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream())
    media.ingest_error = MediaTooLargeError("declared media size exceeds the cap")
    update = _media_update(
        document={"file_id": "doc1", "file_name": "big.pdf", "mime_type": "application/pdf", "file_size": 99_999_999},
    )
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert conversations.accept_calls == []
    assert conversations.rejected_calls[0].kind == "document"
    assert conversations.rejected_calls[0].reason is InboundRejectionReason.TOO_LARGE


async def test_disallowed_media_type_rejects_unsupported_type_and_bridges_caption(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # The ingest seam raising MediaTypeNotAllowedError is a PERMANENT reject mapped to
    # UNSUPPORTED_TYPE; a caption present ALSO bridges as a text-only turn (the served media does not
    # exist, so no media_id/sha/size/filename — only the kind and the vendor-declared mime).
    http_recorder.responder = _getfile_responder(file_path="documents/file_1.svg")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream())
    media.ingest_error = MediaTypeNotAllowedError("svg is active content")
    update = _media_update(
        document={"file_id": "doc1", "file_name": "x.svg", "mime_type": "image/svg+xml", "file_size": 2048},
        caption="have a look",
    )
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}  # the caption turn bridged
    assert conversations.rejected_calls[0].kind == "document"
    assert conversations.rejected_calls[0].reason is InboundRejectionReason.UNSUPPORTED_TYPE
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.text == "have a look"
    assert call.attachments is None
    assert "media_id" not in call.params
    assert call.params["media_kind"] == "document"
    assert call.params["media_mime_type"] == "image/svg+xml"


async def test_media_store_unavailable_rejects_could_not_receive(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # The ingest seam raising MediaStoreUnavailableError (no blob provider configured) is a PERMANENT
    # reject mapped to COULD_NOT_RECEIVE; no caption -> notice + ack only, never a raise/redelivery.
    http_recorder.responder = _getfile_responder()
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(content_type="image/png"))
    media.ingest_error = MediaStoreUnavailableError("no blob provider")
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert conversations.accept_calls == []
    assert conversations.rejected_calls[0].kind == "image"
    assert conversations.rejected_calls[0].reason is InboundRejectionReason.COULD_NOT_RECEIVE


async def test_url_guard_reject_at_open_rejects_could_not_receive(
    http_recorder, fake_redis, conversations, monkeypatch: pytest.MonkeyPatch
):
    # An SSRF UrlGuardError raised while opening the file stream is a PERMANENT reject mapped to
    # COULD_NOT_RECEIVE + ack, never a raise/redelivery.
    http_recorder.responder = _getfile_responder()
    monkeypatch.setattr(
        inbound_media_module,
        "open_media_stream",
        _fake_open_stream(raise_at_open=UrlGuardError("SSRF guard: blocked host")),
    )
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert conversations.accept_calls == []
    assert conversations.rejected_calls[0].reason is InboundRejectionReason.COULD_NOT_RECEIVE


async def test_torn_body_read_raises_for_redelivery(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # A body-read fault surfaced by the seam as MediaSourceReadError is TRANSIENT: it raises so
    # Telegram redelivers, never a permanent reject.
    http_recorder.responder = _getfile_responder()
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream())
    media.ingest_error = MediaSourceReadError("media source read failed: ReadError")
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    with pytest.raises(MediaSourceReadError):
        await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert conversations.rejected_calls == []


async def test_stream_open_transient_fetch_fault_raises(
    http_recorder, fake_redis, conversations, monkeypatch: pytest.MonkeyPatch
):
    # A connect/timeout/5xx at stream open is a transient MediaFetchError (.transient True): raise
    # for redelivery, never a permanent reject.
    http_recorder.responder = _getfile_responder()
    fault = MediaFetchError(host="file.telegram.test", cause_class="ConnectTimeout")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(raise_at_open=fault))
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    with pytest.raises(MediaFetchError):
        await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert conversations.rejected_calls == []


async def test_stream_open_permanent_fetch_fault_rejects_could_not_receive(
    http_recorder, fake_redis, conversations, monkeypatch: pytest.MonkeyPatch
):
    # A 4xx at stream open is a non-transient MediaFetchError (.transient False): a permanent
    # COULD_NOT_RECEIVE reject + ack, never a raise.
    http_recorder.responder = _getfile_responder()
    fault = MediaFetchError(host="file.telegram.test", status_code=403)
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(raise_at_open=fault))
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert conversations.rejected_calls[0].reason is InboundRejectionReason.COULD_NOT_RECEIVE


async def test_fetch_fault_never_leaks_the_bot_token(http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch):
    # The file URL carries the bot token in its PATH. A fetch fault's message and its whole
    # __cause__/__context__ chain must never contain the token (the kit's faults are URL-free).
    http_recorder.responder = _getfile_responder()
    fault = MediaFetchError(host="file.telegram.test", cause_class="ReadError")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(raise_at_open=fault))
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    with pytest.raises(MediaFetchError) as excinfo:
        await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    exc: BaseException | None = excinfo.value
    seen: list[BaseException] = []
    while exc is not None and exc not in seen:
        seen.append(exc)
        assert _BOT_TOKEN not in str(exc)
        exc = exc.__cause__ or exc.__context__


async def test_rejected_media_with_caption_also_bridges_the_caption(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # A permanent reject with a caption present: the participant is notified AND a text-only turn
    # carrying the caption is bridged (the served media does not exist, so no media_id/sha/size/
    # filename and never the raw vendor filename — only the kind and the vendor-declared mime).
    http_recorder.responder = _getfile_responder(file_path="documents/file_1.pdf")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream())
    media.ingest_error = MediaTooLargeError("over cap")
    update = _media_update(
        document={"file_id": "doc1", "file_name": "big.pdf", "mime_type": "application/pdf", "file_size": 99_999_999},
        caption="please review",
    )
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert conversations.rejected_calls[0].reason is InboundRejectionReason.TOO_LARGE
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.text == "please review"
    assert call.attachments is None
    assert "media_id" not in call.params
    assert "media_filename" not in call.params
    assert call.params["media_kind"] == "document"
    assert call.params["media_mime_type"] == "application/pdf"


async def test_media_reply_to_pending_ask_resolves_the_ask_with_ingested_params(
    http_recorder, fake_redis, conversations, channels, media, monkeypatch: pytest.MonkeyPatch
):
    # A photo REPLYING to a pending ForceReply question resolves that ask exactly as a text reply
    # does: the answer text is the caption and the parity media_* params ride the ask (with the
    # SERVED media_id and the seam's sha256 — the answer ladder carries no typed attachment), so the
    # ask is resolved via the ladder and no fresh-turn bridge is made.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED
    http_recorder.responder = _getfile_responder()
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(content_type="image/png"))
    media.ingest_result = _ingested(MediaKind.IMAGE, size=900, mime="image/png")
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}], caption="the blue one", reply_to_message_id=42)
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "forwarded"}}
    assert len(media.ingest_calls) == 1  # the bytes are ingested FIRST, before the resolve/bridge split
    assert len(channels.inbound_calls) == 1
    call = channels.inbound_calls[0]
    assert call.correlation_key == "777:42"  # the replied-to anchor scoped by its chat
    assert call.answer == "the blue one"  # the caption is the answer text
    assert call.bridge.params["media_kind"] == "image"
    assert call.bridge.params["media_id"] == _SERVED_ID  # the SERVED id, never the raw file_id
    assert call.bridge.params["media_sha256"] == media.ingest_result.sha256
    assert conversations.accept_calls == []  # a resolved ask never bridges an accept turn


async def test_media_reply_to_unknown_message_bridges_as_a_turn(
    http_recorder, fake_redis, conversations, channels, media, monkeypatch: pytest.MonkeyPatch
):
    # A photo replying to a message that is NOT a pending ask: the ladder returns NO_CORRELATION and
    # the media turn bridges as a fresh turn, carrying the typed attachment and the parity params.
    channels.inbound_outcome = InboundAnswerOutcome.NO_CORRELATION
    http_recorder.responder = _getfile_responder()
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(content_type="image/png"))
    media.ingest_result = _ingested(MediaKind.IMAGE)
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}], caption="look", reply_to_message_id=99)
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}
    assert len(channels.inbound_calls) == 1  # the ladder was consulted first
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.text == "look"
    assert call.attachments == [media.ingest_result.item]  # the typed attachment rides the bridge alone
    assert call.params["media_id"] == _SERVED_ID


async def test_rejected_media_reply_leaves_the_ask_pending(
    http_recorder, fake_redis, conversations, channels, media, monkeypatch: pytest.MonkeyPatch
):
    # An over-cap photo REPLYING to a pending ask never resolves it: the participant is told the
    # media could not be received (TOO_LARGE) and the ask stays pending (the ladder is never
    # consulted); a caption present is still bridged as a text-only turn so the words are not lost.
    channels.inbound_outcome = InboundAnswerOutcome.FORWARDED  # would resolve if ever consulted
    http_recorder.responder = _getfile_responder()
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream())
    media.ingest_error = MediaTooLargeError("over cap")
    update = _media_update(
        photo=[{"file_id": "big", "file_size": 99_999_999}], caption="please review", reply_to_message_id=42
    )
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "accepted"}}  # the caption turn bridged
    assert channels.inbound_calls == []  # the ask was never resolved
    assert conversations.rejected_calls[0].reason is InboundRejectionReason.TOO_LARGE
    assert len(conversations.accept_calls) == 1
    call = conversations.accept_calls[0]
    assert call.text == "please review"
    assert call.attachments is None
    assert "media_id" not in call.params


async def test_telegram_parity_media_filename_equals_sanitised(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # A document whose vendor file_name carries a control char and a bidi override: the parity
    # media_filename is the seam's SANITISED name, and the raw vendor name is in NO param value.
    raw_name = "re‮port\x00.pdf"
    sanitised = "report.pdf"
    http_recorder.responder = _getfile_responder(file_path="documents/file_1.pdf")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream())
    media.ingest_result = _ingested(MediaKind.DOCUMENT, mime="application/pdf", filename=sanitised)
    update = _media_update(
        document={"file_id": "doc1", "file_name": raw_name, "mime_type": "application/pdf", "file_size": 2048},
    )
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    call = conversations.accept_calls[0]
    assert call.params["media_filename"] == sanitised
    assert all(raw_name not in value and "‮" not in value for value in call.params.values())
    # The seam receives the RAW vendor filename to sanitise; the raw name never leaves via params.
    assert media.ingest_calls[0].filename == raw_name


async def test_telegram_placeholder_label_uses_sanitised_filename(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # The same document with no caption: the bridged turn text is "[document: <sanitised>]" and
    # the raw vendor file_name appears nowhere in the turn text or params.
    raw_name = "re‮port\x00.pdf"
    sanitised = "report.pdf"
    http_recorder.responder = _getfile_responder(file_path="documents/file_1.pdf")
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream())
    media.ingest_result = _ingested(MediaKind.DOCUMENT, mime="application/pdf", filename=sanitised)
    update = _media_update(
        document={"file_id": "doc1", "file_name": raw_name, "mime_type": "application/pdf", "file_size": 2048},
    )
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    call = conversations.accept_calls[0]
    assert call.text == f"[document: {sanitised}]"
    assert raw_name not in call.text
    assert all(raw_name not in value for value in call.params.values())


async def test_media_fires_typing_before_fetch(
    http_recorder, fake_redis, conversations, media, monkeypatch: pytest.MonkeyPatch
):
    # A media message is a processable message: the "working on it" typing action fires before the
    # fetch/ingest, exactly as for a text message.
    http_recorder.responder = _getfile_responder()
    monkeypatch.setattr(inbound_media_module, "open_media_stream", _fake_open_stream(content_type="image/png"))
    media.ingest_result = _ingested(MediaKind.IMAGE)
    update = _media_update(photo=[{"file_id": "big", "file_size": 900}])
    await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    typing = [r for r in http_recorder.requests if str(r.url).endswith("/sendChatAction")]
    assert len(typing) == 1


async def test_unmappable_content_notifies_rejection_and_makes_no_turn(http_recorder, fake_redis, conversations):
    # A poll is content Telegram sends that this channel cannot map to a turn: the door routes it
    # to the shared notify_inbound_rejected chokepoint (one generic notice + operator event) and
    # acks it, never a silent drop and never a bridged turn.
    update = _media_update(poll={"id": "p1", "question": "which?"})
    response = await inbound(make_inbound_request(update, headers=_VALID_HEADERS))
    assert response.status_code == 200
    assert _body(response) == {"data": {"status": "ignored"}}
    assert conversations.accept_calls == []
    assert len(conversations.rejected_calls) == 1
    rejected = conversations.rejected_calls[0]
    assert rejected.channel_id == "telegram"
    assert rejected.recipient == "777"
    assert rejected.sender_identity == "123456"
    assert rejected.kind == "poll"
    assert rejected.reason is InboundRejectionReason.UNSUPPORTED_TYPE
