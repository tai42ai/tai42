"""TelegramChannel.deliver and .notify: outbound payload shape and every failure branch, offline."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification, Correlation
from tai42_contract.interactions.models import MediaItem, MediaKind
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.channel import TelegramChannel

_CALLBACK = "https://example.test/api/interactions/callback/tkt"
_TOKEN = "123456:test-token"


def _stored_correlation(fake_redis, key: str = "channel:telegram:corr:777:42") -> Correlation:
    """The :class:`Correlation` the store persisted under ``key`` (a JSON record now,
    carrying callback_url + interaction_id + ttl_deadline). The key is chat-scoped:
    ``channel:telegram:corr:{chat_id}:{message_id}`` — the default target chat is 777."""
    return Correlation.model_validate_json(fake_redis.data[key])


def _delivery(
    answer_format: str = "text",
    options: list[str] | None = None,
    timeout_in: float = 600,
    recipient: str | None = None,
    schema: dict | None = None,
) -> ChannelDelivery:
    return ChannelDelivery(
        interaction_id="int-1",
        question="Which one?",
        answer_format=answer_format,
        options=options,
        schema=schema,
        callback_url=_CALLBACK,
        timeout_at=datetime.now(UTC) + timedelta(seconds=timeout_in),
        recipient=recipient,
    )


_FORM_SCHEMA = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}


async def test_text_ask_sends_force_reply_and_stores_correlation(http_recorder, fake_redis):
    await TelegramChannel().deliver(_delivery())

    assert len(http_recorder.requests) == 1
    request = http_recorder.requests[0]
    assert str(request.url) == f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
    body = json.loads(request.content)
    # No caller recipient -> the operator default is the target chat.
    assert body["chat_id"] == "777"
    assert body["text"].startswith("Which one?")
    assert "(Answer before " in body["text"]
    assert body["reply_markup"] == {"force_reply": True, "input_field_placeholder": "Reply to answer"}

    # The store now persists a Correlation record (callback_url + interaction_id +
    # ttl_deadline), keyed by the chat-scoped anchor {chat_id}:{message_id}, TTL = the
    # remaining budget.
    assert list(fake_redis.data) == ["channel:telegram:corr:777:42"]
    entry = _stored_correlation(fake_redis)
    assert entry.callback_url == _CALLBACK
    assert entry.interaction_id == "int-1"
    assert 599 <= fake_redis.ttls["channel:telegram:corr:777:42"] <= 601


async def test_select_renders_options_as_inline_keyboard(http_recorder, fake_redis):
    # Clean break from numbered text: a select ask renders its options as a native
    # inline keyboard, one callback button per option (callback_data = the index),
    # and keeps the option list in a side record so an inbound tap maps back to text.
    await TelegramChannel().deliver(_delivery(answer_format="select", options=["red", "blue"]))

    body = json.loads(http_recorder.requests[0].content)
    # No numbered/guided option text — the buttons carry the choices.
    assert "Reply with one of the options above." not in body["text"]
    assert body["text"].startswith("Which one?")
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "red", "callback_data": "0"}],
            [{"text": "blue", "callback_data": "1"}],
        ]
    }
    assert "force_reply" not in json.dumps(body)
    # Both the correlation (for the ladder) and the option side record are stored,
    # keyed by the chat-scoped anchor {chat_id}:{message_id}, TTL = the remaining budget.
    assert set(fake_redis.data) == {"channel:telegram:corr:777:42", "channel:telegram:opts:777:42"}
    assert _stored_correlation(fake_redis).callback_url == _CALLBACK
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == ["red", "blue"]
    assert 599 <= fake_redis.ttls["channel:telegram:opts:777:42"] <= 601


async def test_text_ask_with_suggested_replies_renders_inline_keyboard(http_recorder, fake_redis):
    # A text ask MAY carry suggested replies (contract): they render as the same
    # inline keyboard, and a tap submits the option text as the free-text answer.
    await TelegramChannel().deliver(_delivery(answer_format="text", options=["yes please", "no thanks"]))

    body = json.loads(http_recorder.requests[0].content)
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "yes please", "callback_data": "0"}],
            [{"text": "no thanks", "callback_data": "1"}],
        ]
    }
    assert set(fake_redis.data) == {"channel:telegram:corr:777:42", "channel:telegram:opts:777:42"}
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == ["yes please", "no thanks"]


async def test_deliver_sends_image_media_then_question(http_recorder, fake_redis):
    # An image media item is sent as its own sendPhoto message BEFORE the question;
    # a link item is appended to the question text as a labelled line.
    media = [
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption="a diagram"),
        MediaItem(kind=MediaKind.LINK, url="https://docs.test/spec", caption="the spec"),
    ]
    delivery = ChannelDelivery(
        interaction_id="int-1",
        question="Approve?",
        answer_format="text",
        media=media,
        callback_url=_CALLBACK,
        timeout_at=datetime.now(UTC) + timedelta(seconds=600),
    )
    await TelegramChannel().deliver(delivery)

    assert [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests] == ["sendPhoto", "sendMessage"]
    photo = json.loads(http_recorder.requests[0].content)
    assert photo == {"chat_id": "777", "photo": "https://cdn.test/a.png", "caption": "a diagram"}
    text = json.loads(http_recorder.requests[1].content)["text"]
    assert text.startswith("Approve?")
    assert "the spec: https://docs.test/spec" in text


async def test_deliver_rejects_data_uri_image_before_any_send(http_recorder, fake_redis):
    from tai42_contract.channels import ChannelInputError

    media = [MediaItem(kind=MediaKind.IMAGE, url="data:image/png;base64,AAAA", caption=None)]
    delivery = ChannelDelivery(
        interaction_id="int-1",
        question="Approve?",
        answer_format="text",
        media=media,
        callback_url=_CALLBACK,
        timeout_at=datetime.now(UTC) + timedelta(seconds=600),
    )
    with pytest.raises(ChannelInputError, match="data: image"):
        await TelegramChannel().deliver(delivery)
    # Refused up front: nothing was sent and no correlation was stored.
    assert http_recorder.requests == []
    assert fake_redis.data == {}


@pytest.mark.parametrize("answer_format", ["confirm", "external"])
async def test_tier1_sends_url_button_and_skips_correlation(http_recorder, fake_redis, answer_format: str):
    await TelegramChannel().deliver(_delivery(answer_format=answer_format))

    body = json.loads(http_recorder.requests[0].content)
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "Answer", "url": _CALLBACK}]]}
    assert "force_reply" not in json.dumps(body)
    assert "Reply with" not in body["text"]
    assert body["text"].startswith("Which one?")
    assert "(Answer before " in body["text"]
    assert fake_redis.data == {}


def test_channel_advertises_form_delivery():
    # The capability flag the ask helper reads before handing a form ticket here.
    assert TelegramChannel.supports_form_delivery is True


async def test_form_sends_web_app_button_and_skips_correlation(http_recorder, fake_redis):
    await TelegramChannel().deliver(_delivery(answer_format="form", schema=_FORM_SCHEMA))

    assert len(http_recorder.requests) == 1
    request = http_recorder.requests[0]
    assert str(request.url) == f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
    body = json.loads(request.content)
    assert body["chat_id"] == "777"
    assert body["text"].startswith("Which one?")
    assert "(Answer before " in body["text"]
    # No options text — a form frame carries none.
    assert "Reply with" not in body["text"]
    # A web_app button (in-chat webview), not a url button; no force_reply.
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "Fill form", "web_app": {"url": _CALLBACK}}]]}
    assert "force_reply" not in json.dumps(body)
    # The callback page posts the answer itself: no correlation, no inbound leg.
    assert fake_redis.data == {}
    assert fake_redis.ttls == {}


async def test_already_expired_raises_without_sending(http_recorder, fake_redis):
    with pytest.raises(ChannelDeliveryError, match="already timed out"):
        await TelegramChannel().deliver(_delivery(timeout_in=-10))
    assert http_recorder.requests == []
    assert fake_redis.data == {}


async def test_transport_error_single_attempt_token_free(http_recorder, fake_redis):
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    http_recorder.responder = responder
    with pytest.raises(ChannelDeliveryError, match="sendMessage failed") as excinfo:
        await TelegramChannel().deliver(_delivery())
    assert len(http_recorder.requests) == 1
    assert _TOKEN not in str(excinfo.value)


async def test_http_error_status_raises(http_recorder, fake_redis):
    http_recorder.responder = lambda request: httpx.Response(500, text="server error")
    with pytest.raises(ChannelDeliveryError, match="HTTP 500") as excinfo:
        await TelegramChannel().deliver(_delivery())
    assert "int-1" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)


async def test_non_json_body_raises(http_recorder, fake_redis):
    http_recorder.responder = lambda request: httpx.Response(200, text="not json")
    with pytest.raises(ChannelDeliveryError, match="non-JSON body") as excinfo:
        await TelegramChannel().deliver(_delivery())
    assert "int-1" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)


async def test_ok_false_echoes_error_code_and_description(http_recorder, fake_redis):
    http_recorder.responder = lambda request: httpx.Response(
        200, json={"ok": False, "error_code": 429, "description": "Too Many Requests"}
    )
    with pytest.raises(ChannelDeliveryError, match="error_code=429") as excinfo:
        await TelegramChannel().deliver(_delivery())
    assert "Too Many Requests" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)


@pytest.mark.parametrize(
    "body",
    [
        {"ok": True},
        {"ok": True, "result": None},
        {"ok": True, "result": {}},
        {"ok": True, "result": {"message_id": "42"}},
    ],
)
async def test_ok_true_without_message_id_raises(http_recorder, fake_redis, body: dict):
    http_recorder.responder = lambda request: httpx.Response(200, json=body)
    with pytest.raises(ChannelDeliveryError, match=r"carried no result\.message_id") as excinfo:
        await TelegramChannel().deliver(_delivery())
    assert "int-1" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)
    assert fake_redis.data == {}


@pytest.mark.parametrize(
    "result", [{"message_id": 42}, {"message_id": 42, "chat": None}, {"message_id": 42, "chat": {}}]
)
async def test_ok_true_without_chat_id_raises(http_recorder, fake_redis, result: dict):
    # A send that reports its message_id but no numeric result.chat.id is a loud error:
    # the anchor is keyed by the authoritative chat id, so a reply could never be routed
    # back without it. Nothing is stored.
    http_recorder.responder = lambda request: httpx.Response(200, json={"ok": True, "result": result})
    with pytest.raises(ChannelDeliveryError, match=r"carried no result\.chat\.id") as excinfo:
        await TelegramChannel().deliver(_delivery())
    assert "int-1" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)
    assert fake_redis.data == {}


async def test_deliver_options_store_failure_is_loud(http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch):
    # The correlation is stored, but persisting the option side record fails: a select
    # ask's button taps could never be routed, so the send raises rather than silently
    # dropping the keyboard mapping (a typed reply would still resolve via the correlation).
    async def broken_set_options(*args: object, **kwargs: object) -> None:
        raise RuntimeError("opts store down")

    monkeypatch.setattr("tai42_channel_telegram.channel.set_options", broken_set_options)
    with pytest.raises(ChannelDeliveryError, match="button taps cannot be routed") as excinfo:
        await TelegramChannel().deliver(_delivery(answer_format="select", options=["red", "blue"]))
    assert "was sent" in str(excinfo.value)


async def test_notify_options_store_failure_is_loud(http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch):
    # A notify's options render as a keyboard whose taps bridge via the side record;
    # a failure persisting that record is loud (the taps could never be routed).
    async def broken_set_options(*args: object, **kwargs: object) -> None:
        raise RuntimeError("opts store down")

    monkeypatch.setattr("tai42_channel_telegram.channel.set_options", broken_set_options)
    with pytest.raises(ChannelDeliveryError, match="button taps cannot be routed") as excinfo:
        await TelegramChannel().notify(ChannelNotification(message="Pick one:", options=["a", "b"]))
    assert "was sent" in str(excinfo.value)


async def test_budget_spent_during_send_raises_without_storing(http_recorder, fake_redis):
    def slow_responder(request: httpx.Request) -> httpx.Response:
        time.sleep(0.2)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": 777}}})

    http_recorder.responder = slow_responder
    with pytest.raises(ChannelDeliveryError, match="cannot be routed"):
        await TelegramChannel().deliver(_delivery(timeout_in=0.05))
    assert len(http_recorder.requests) == 1
    assert fake_redis.data == {}


async def test_correlation_store_failure_is_loud(http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch):
    async def broken_set(key: str, value: str, ex: int | None = None) -> None:
        raise RuntimeError("redis down")

    monkeypatch.setattr(fake_redis, "set", broken_set)
    with pytest.raises(ChannelDeliveryError, match="cannot be routed") as excinfo:
        await TelegramChannel().deliver(_delivery())
    assert "was sent" in str(excinfo.value)


async def test_missing_bot_token_raises_naming_var_before_any_other_check(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # The delivery also names an unlisted recipient AND an expired deadline —
    # the token check runs first, so the config error is the one that raises.
    monkeypatch.delenv("CHANNEL_TELEGRAM_BOT_TOKEN")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TELEGRAM_BOT_TOKEN"):
        await TelegramChannel().deliver(_delivery(recipient="666", timeout_in=-10))
    assert http_recorder.requests == []


async def test_caller_recipient_on_allowlist_sends_to_it(http_recorder, fake_redis):
    await TelegramChannel().deliver(_delivery(recipient="888"))

    assert len(http_recorder.requests) == 1
    body = json.loads(http_recorder.requests[0].content)
    assert body["chat_id"] == "888"
    # The anchor is scoped by the target chat 888, not the operator default.
    assert list(fake_redis.data) == ["channel:telegram:corr:888:42"]
    assert _stored_correlation(fake_redis, "channel:telegram:corr:888:42").callback_url == _CALLBACK


async def test_at_username_recipient_scopes_keys_by_numeric_response_chat_id(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # REGRESSION: the writer scoped its anchors by the configured recipient STRING, so an
    # ``@username`` recipient wrote keys under ``@username:{message_id}`` while the inbound
    # reader derives the NUMERIC ``chat.id`` from the update — the two never matched and the
    # reply/tap could not resolve. The writer now scopes by the response's authoritative
    # numeric ``result.chat.id``, so an ``@username`` delivery's keys use the numeric id.
    monkeypatch.setenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS", "@mychannel")
    reset_all_settings()
    numeric_chat_id = -1001234567890  # what Telegram resolves @mychannel to, returned in result.chat.id

    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42, "chat": {"id": numeric_chat_id}}})

    http_recorder.responder = responder

    await TelegramChannel().deliver(_delivery(answer_format="select", options=["red", "blue"], recipient="@mychannel"))

    # The send addressed the @username verbatim ...
    assert json.loads(http_recorder.requests[0].content)["chat_id"] == "@mychannel"
    # ... but both anchors key by the NUMERIC chat id from the response, never "@mychannel".
    corr_key = f"channel:telegram:corr:{numeric_chat_id}:42"
    opts_key = f"channel:telegram:opts:{numeric_chat_id}:42"
    assert set(fake_redis.data) == {corr_key, opts_key}
    assert not any("@mychannel" in k for k in fake_redis.data)

    # Reader resolution: the inbound door derives the same numeric id from an update, so its
    # scoped-key lookups (via the same helpers) hit exactly these records.
    from tai42_channel_telegram.correlation import get_options, scoped_correlation_key, telegram_correlation_store

    resolved = await telegram_correlation_store.get_correlation(scoped_correlation_key(str(numeric_chat_id), "42"))
    assert resolved is not None
    assert resolved.interaction_id == "int-1"
    assert await get_options(str(numeric_chat_id), "42") == ["red", "blue"]


async def test_caller_recipient_not_on_allowlist_refuses_without_sending(http_recorder, fake_redis):
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS"):
        await TelegramChannel().deliver(_delivery(recipient="666"))
    assert http_recorder.requests == []
    assert fake_redis.data == {}


async def test_caller_recipient_with_empty_allowlist_refuses(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # An empty allowlist allows NO caller-supplied recipient — not even the
    # operator default's own address (the default is trusted only when the
    # caller names nothing).
    monkeypatch.delenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS"):
        await TelegramChannel().deliver(_delivery(recipient="777"))
    assert http_recorder.requests == []
    assert fake_redis.data == {}


async def test_no_recipient_and_no_default_raises_naming_var(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("CHANNEL_TELEGRAM_DEFAULT_RECIPIENT")
    monkeypatch.delenv("CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TELEGRAM_DEFAULT_RECIPIENT"):
        await TelegramChannel().deliver(_delivery())
    assert http_recorder.requests == []


async def test_notify_sends_plain_payload_to_default_without_correlation(http_recorder, fake_redis):
    await TelegramChannel().notify(ChannelNotification(message="Deploy finished."))

    assert len(http_recorder.requests) == 1
    request = http_recorder.requests[0]
    assert str(request.url) == f"https://api.telegram.org/bot{_TOKEN}/sendMessage"
    # Exactly chat_id + text: no reply_markup, no force_reply, no url button.
    assert json.loads(request.content) == {"chat_id": "777", "text": "Deploy finished."}
    # Fire-and-forget: nothing is written to the correlation store.
    assert fake_redis.data == {}
    assert fake_redis.ttls == {}


async def test_notify_caller_recipient_on_allowlist_sends_to_it(http_recorder, fake_redis):
    await TelegramChannel().notify(ChannelNotification(message="ping", recipient="888"))

    assert len(http_recorder.requests) == 1
    assert json.loads(http_recorder.requests[0].content) == {"chat_id": "888", "text": "ping"}
    assert fake_redis.data == {}


async def test_notify_missing_bot_token_raises_naming_var_before_any_other_check(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # The notification also names an unlisted recipient — the token check runs
    # first, so the config error is the one that raises.
    monkeypatch.delenv("CHANNEL_TELEGRAM_BOT_TOKEN")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError, match="set CHANNEL_TELEGRAM_BOT_TOKEN"):
        await TelegramChannel().notify(ChannelNotification(message="ping", recipient="666"))
    assert http_recorder.requests == []


async def test_notify_caller_recipient_not_on_allowlist_refuses_without_sending(http_recorder, fake_redis):
    with pytest.raises(ChannelDeliveryError, match="not on CHANNEL_TELEGRAM_ALLOWED_RECIPIENTS"):
        await TelegramChannel().notify(ChannelNotification(message="ping", recipient="666"))
    assert http_recorder.requests == []
    assert fake_redis.data == {}


@pytest.mark.parametrize(
    ("responder", "match"),
    [
        pytest.param(lambda request: httpx.Response(500, text="server error"), "HTTP 500", id="http-500"),
        pytest.param(lambda request: httpx.Response(200, text="not json"), "non-JSON body", id="non-json"),
        pytest.param(
            lambda request: httpx.Response(200, json={"ok": False, "error_code": 403, "description": "Forbidden"}),
            "error_code=403",
            id="ok-false",
        ),
    ],
)
async def test_notify_failure_response_raises_token_free(http_recorder, fake_redis, responder, match: str):
    http_recorder.responder = responder
    with pytest.raises(ChannelDeliveryError, match=match) as excinfo:
        await TelegramChannel().notify(ChannelNotification(message="ping"))
    assert "notification" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)
    assert fake_redis.data == {}


async def test_notify_returns_sent_message_id(http_recorder, fake_redis):
    # The default responder mints message_id 42; notify returns it as [str].
    result = await TelegramChannel().notify(ChannelNotification(message="Deploy finished."))
    assert result == ["42"]


async def test_notify_ok_without_message_id_raises(http_recorder, fake_redis):
    http_recorder.responder = lambda request: httpx.Response(200, json={"ok": True, "result": {}})
    with pytest.raises(ChannelDeliveryError, match=r"carried no result\.message_id") as excinfo:
        await TelegramChannel().notify(ChannelNotification(message="ping"))
    assert "notification" in str(excinfo.value)


async def test_notify_sender_identity_matches_sends_verbatim_and_returns_id(http_recorder, fake_redis):
    # sender_identity = this bot's numeric id -> send to the given recipient
    # verbatim (allowlist bypassed for the bridge reply), returning the message id.
    result = await TelegramChannel().notify(
        ChannelNotification(message="reply", recipient="555", sender_identity="123456")
    )
    assert result == ["42"]
    assert json.loads(http_recorder.requests[0].content) == {"chat_id": "555", "text": "reply"}


async def test_notify_sender_identity_mismatch_raises_without_sending(http_recorder, fake_redis):
    with pytest.raises(ChannelDeliveryError, match="is not this bot's identity"):
        await TelegramChannel().notify(ChannelNotification(message="reply", recipient="555", sender_identity="999999"))
    assert http_recorder.requests == []


async def test_notify_sender_identity_with_malformed_token_raises(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # Deriving this bot's identity from a token with no numeric prefix is a loud
    # typed error, never a silent send.
    monkeypatch.setenv("CHANNEL_TELEGRAM_BOT_TOKEN", "no-colon-token")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError, match="CHANNEL_TELEGRAM_BOT_TOKEN is malformed"):
        await TelegramChannel().notify(ChannelNotification(message="reply", recipient="555", sender_identity="123456"))
    assert http_recorder.requests == []


def test_channel_advertises_media_and_interactive_notifications():
    # The central notify guard reads these before handing a media / options
    # notification to this channel.
    assert TelegramChannel.supports_media_notifications is True
    assert TelegramChannel.supports_interactive_notifications is True


async def test_notify_sends_body_then_photos_returning_every_id(http_recorder, fake_redis):
    # The body (with link items appended) goes first as one sendMessage, then each
    # image item as its own sendPhoto; every minted message id is returned in order.
    media = [
        MediaItem(kind=MediaKind.LINK, url="https://docs.test/x", caption="doc"),
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption="pic a"),
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/b.png", caption=None),
    ]
    result = await TelegramChannel().notify(ChannelNotification(message="See below.", media=media))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendMessage", "sendPhoto", "sendPhoto"]
    body = json.loads(http_recorder.requests[0].content)
    assert body["text"] == "See below.\ndoc: https://docs.test/x"
    assert "reply_markup" not in body  # no options -> no keyboard
    assert json.loads(http_recorder.requests[1].content) == {
        "chat_id": "777",
        "photo": "https://cdn.test/a.png",
        "caption": "pic a",
    }
    # A captionless image sends no caption key.
    assert json.loads(http_recorder.requests[2].content) == {"chat_id": "777", "photo": "https://cdn.test/b.png"}
    assert result == ["42", "42", "42"]
    # Fire-and-forget: no correlation is stored (no options here).
    assert fake_redis.data == {}


async def test_notify_media_only_images_skip_the_sendmessage(http_recorder, fake_redis):
    # A media-only notification (blank message, images only) sends NO text sendMessage — just a
    # sendPhoto per image. Telegram rejects an empty text, so the message is skipped entirely.
    media = [
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption="pic a"),
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/b.png", caption=None),
    ]
    result = await TelegramChannel().notify(ChannelNotification(message="", media=media))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendPhoto", "sendPhoto"]  # no sendMessage
    assert result == ["42", "42"]


async def test_notify_media_only_with_a_link_renders_the_link_as_the_body(http_recorder, fake_redis):
    # A media-only notification whose media carries a link renders that link AS the sendMessage
    # text (no leading blank line from the empty message), then the image sendPhoto(s).
    media = [
        MediaItem(kind=MediaKind.LINK, url="https://docs.test/x", caption="doc"),
        MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption=None),
    ]
    result = await TelegramChannel().notify(ChannelNotification(message="", media=media))

    methods = [str(r.url).rsplit("/", 1)[-1] for r in http_recorder.requests]
    assert methods == ["sendMessage", "sendPhoto"]
    body = json.loads(http_recorder.requests[0].content)
    assert body["text"] == "doc: https://docs.test/x"
    assert result == ["42", "42"]


async def test_notify_options_render_keyboard_and_store_side_record(http_recorder, fake_redis):
    # Notify options render as an inline keyboard on the body message; the option
    # list is kept in a side record (keyed by the anchor message id) so a later tap
    # bridges the option text, with the operator-set notify TTL.
    result = await TelegramChannel().notify(ChannelNotification(message="Pick one:", options=["a", "b", "c"]))

    body = json.loads(http_recorder.requests[0].content)
    assert body["reply_markup"] == {
        "inline_keyboard": [
            [{"text": "a", "callback_data": "0"}],
            [{"text": "b", "callback_data": "1"}],
            [{"text": "c", "callback_data": "2"}],
        ]
    }
    assert result == ["42"]
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == ["a", "b", "c"]
    assert fake_redis.ttls["channel:telegram:opts:777:42"] == 86_400  # the default option-tap TTL
    # No correlation record — a notify has no callback/ask.
    assert "channel:telegram:corr:777:42" not in fake_redis.data


async def test_notify_data_uri_image_is_refused_before_any_send(http_recorder, fake_redis):
    from tai42_contract.channels import ChannelInputError

    media = [MediaItem(kind=MediaKind.IMAGE, url="data:image/png;base64,AAAA", caption=None)]
    with pytest.raises(ChannelInputError, match="data: image"):
        await TelegramChannel().notify(ChannelNotification(message="hi", media=media))
    assert http_recorder.requests == []


async def test_notify_photo_failure_after_body_names_delivered_ids(http_recorder, fake_redis):
    # A photo that fails mid-send raises naming the ids already delivered (the body),
    # so a partial multi-part send stays visible.
    def responder(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/sendPhoto"):
            return httpx.Response(200, json={"ok": False, "error_code": 400, "description": "bad photo"})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    http_recorder.responder = responder
    media = [MediaItem(kind=MediaKind.IMAGE, url="https://cdn.test/a.png", caption=None)]
    with pytest.raises(ChannelDeliveryError, match=r"after delivering \['42'\]"):
        await TelegramChannel().notify(ChannelNotification(message="hi", media=media))


# --- send_chat_action ("working on it" typing signal) ------------------------


async def test_send_chat_action_posts_exact_body(http_recorder, fake_redis):
    from tai42_channel_telegram.client import send_chat_action

    await send_chat_action(555, "typing")
    assert len(http_recorder.requests) == 1
    request = http_recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telegram.org/bot{_TOKEN}/sendChatAction"
    assert json.loads(request.content) == {"chat_id": 555, "action": "typing"}


async def test_send_chat_action_ok_false_raises(http_recorder, fake_redis):
    from tai42_channel_telegram.client import send_chat_action

    http_recorder.responder = lambda request: httpx.Response(200, json={"ok": False, "description": "no"})
    with pytest.raises(ChannelDeliveryError, match="sendChatAction rejected"):
        await send_chat_action(555, "typing")


async def test_send_chat_action_unset_token_raises_delivery_error(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # An unset token is a ChannelDeliveryError (not a raw ValueError) so the inbound
    # door's `except ChannelDeliveryError` swallows it and the webhook still acks.
    monkeypatch.delenv("CHANNEL_TELEGRAM_BOT_TOKEN")
    reset_all_settings()
    from tai42_channel_telegram.client import send_chat_action

    with pytest.raises(ChannelDeliveryError, match="CHANNEL_TELEGRAM_BOT_TOKEN"):
        await send_chat_action(555, "typing")
    assert http_recorder.requests == []
