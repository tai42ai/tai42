"""TelegramChannel.deliver and .notify: outbound payload shape and every failure branch, offline."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from tai42_contract.channels import ChannelDelivery, ChannelDeliveryError, ChannelNotification
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.channel import TelegramChannel

_CALLBACK = "https://example.test/api/interactions/callback/tkt"
_TOKEN = "123456:test-token"


def _delivery(
    answer_format: str = "text",
    options: list[str] | None = None,
    timeout_in: float = 600,
    recipient: str | None = None,
) -> ChannelDelivery:
    return ChannelDelivery(
        interaction_id="int-1",
        question="Which one?",
        answer_format=answer_format,
        options=options,
        callback_url=_CALLBACK,
        timeout_at=datetime.now(UTC) + timedelta(seconds=timeout_in),
        recipient=recipient,
    )


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

    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}
    assert 599 <= fake_redis.ttls["channel:telegram:corr:42"] <= 601


async def test_select_renders_options_as_guided_text(http_recorder, fake_redis):
    await TelegramChannel().deliver(_delivery(answer_format="select", options=["red", "blue"]))

    body = json.loads(http_recorder.requests[0].content)
    assert "- red" in body["text"]
    assert "- blue" in body["text"]
    assert "Reply with one of the options above." in body["text"]
    assert body["reply_markup"] == {"force_reply": True, "input_field_placeholder": "Reply to answer"}
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}


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


async def test_budget_spent_during_send_raises_without_storing(http_recorder, fake_redis):
    def slow_responder(request: httpx.Request) -> httpx.Response:
        time.sleep(0.2)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

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
    assert fake_redis.data == {"channel:telegram:corr:42": _CALLBACK}


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
