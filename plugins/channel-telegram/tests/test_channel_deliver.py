"""Telegram channel delivery: ask/select/media/form sends, correlation storage,
error surfacing, and recipient scoping and allowlisting.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from tai42_contract.channels import (
    ChannelDelivery,
    ChannelDeliveryError,
    ChannelNotification,
    Correlation,
    Option,
    ReplyOption,
)
from tai42_contract.interactions.models import MediaItem, MediaKind
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.channel import TelegramChannel

from .conftest import _TOKEN, _records

_CALLBACK = "https://example.test/api/interactions/callback/tkt"


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
    data: Any = None,
    pages: Any = None,
) -> ChannelDelivery:
    return ChannelDelivery(
        interaction_id="int-1",
        question="Which one?",
        answer_format=answer_format,
        options=options,
        schema=schema,
        data=data,
        pages=pages,
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
    # A select ask renders its options as a native
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
    # The side record maps each button's wire token back to its option; an ask carries no
    # author-set ids, so the token is the index and id/description are null.
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == _records(
        ("0", "red", None, None), ("1", "blue", None, None)
    )
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
    assert json.loads(fake_redis.data["channel:telegram:opts:777:42"]) == _records(
        ("0", "yes please", None, None), ("1", "no thanks", None, None)
    )


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


async def test_form_with_per_send_data_and_pages_is_pass_through_to_the_callback_page(http_recorder, fake_redis):
    # Telegram delivers a form as a web_app button opening the callback page — the page
    # renders the per-send values/options/pages, so the delivery ignores them and the
    # button is unchanged. The new fields must NOT disturb the send or be dropped loudly.
    from tai42_contract.interactions.models import FormData, FormOption, FormPage

    delivery = _delivery(
        answer_format="form",
        schema=_FORM_SCHEMA,
        data=FormData(values={"note": "hi"}, options={"name": [FormOption(value="r", label="Red")]}),
        pages=[FormPage(title="Step", fields=list(_FORM_SCHEMA["properties"]))],
    )
    await TelegramChannel().deliver(delivery)

    body = json.loads(http_recorder.requests[0].content)
    assert body["reply_markup"] == {"inline_keyboard": [[{"text": "Fill form", "web_app": {"url": _CALLBACK}}]]}
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
    options: list[Option] = [ReplyOption(text="a"), ReplyOption(text="b")]
    with pytest.raises(ChannelDeliveryError, match="button taps cannot be routed") as excinfo:
        await TelegramChannel().notify(ChannelNotification(message="Pick one:", options=options))
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
    stored = await get_options(str(numeric_chat_id), "42")
    assert stored is not None
    assert [(option.callback_data, option.text) for option in stored] == [("0", "red"), ("1", "blue")]


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
