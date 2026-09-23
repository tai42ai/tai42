"""The outbound Bot API helpers ``send_chat_action`` and ``answer_callback_query``:
every failure branch, offline.

The happy paths and a couple of failure branches are exercised through the inbound
and channel suites; this module pins the remaining error branches each helper owns —
a transport fault, a non-JSON body, an unset token, and an ``ok: false`` rejection —
so a regression that dropped a raise (and delivered a silent failure) fails a test.
Each asserts the raise is a :class:`ChannelDeliveryError` (what the inbound door's
``except ChannelDeliveryError`` swallows) and that the bot token never leaks into the
error text.
"""

from __future__ import annotations

import httpx
import pytest
from tai42_contract.channels import ChannelDeliveryError
from tai42_kit.settings import reset_all_settings

from tai42_channel_telegram.client import answer_callback_query, send_chat_action

_TOKEN = "123456:test-token"


async def test_send_chat_action_transport_error_raises_token_free(http_recorder, fake_redis):
    # A transport fault (httpx.HTTPError) is wrapped in a ChannelDeliveryError naming
    # the method, and the token never appears in the text.
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    http_recorder.responder = responder
    with pytest.raises(ChannelDeliveryError, match="sendChatAction failed") as excinfo:
        await send_chat_action(555, "typing")
    assert _TOKEN not in str(excinfo.value)


async def test_send_chat_action_non_json_body_raises(http_recorder, fake_redis):
    # A 200 with a non-JSON body is a loud ChannelDeliveryError, never a swallowed miss.
    http_recorder.responder = lambda request: httpx.Response(200, text="not json")
    with pytest.raises(ChannelDeliveryError, match="non-JSON body") as excinfo:
        await send_chat_action(555, "typing")
    assert _TOKEN not in str(excinfo.value)


async def test_answer_callback_query_posts_exact_body(http_recorder, fake_redis):
    # The happy path: one POST to answerCallbackQuery carrying only the query id.
    await answer_callback_query("cb-1")
    assert len(http_recorder.requests) == 1
    request = http_recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == f"https://api.telegram.org/bot{_TOKEN}/answerCallbackQuery"
    import json

    assert json.loads(request.content) == {"callback_query_id": "cb-1"}


async def test_answer_callback_query_unset_token_raises_delivery_error(
    http_recorder, fake_redis, monkeypatch: pytest.MonkeyPatch
):
    # An unset token is a ChannelDeliveryError (not a raw ValueError) so the inbound
    # door's `except ChannelDeliveryError` swallows it and the tap still resolves.
    monkeypatch.delenv("CHANNEL_TELEGRAM_BOT_TOKEN")
    reset_all_settings()
    with pytest.raises(ChannelDeliveryError, match="CHANNEL_TELEGRAM_BOT_TOKEN"):
        await answer_callback_query("cb-1")
    assert http_recorder.requests == []


async def test_answer_callback_query_transport_error_raises_token_free(http_recorder, fake_redis):
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    http_recorder.responder = responder
    with pytest.raises(ChannelDeliveryError, match="answerCallbackQuery failed") as excinfo:
        await answer_callback_query("cb-1")
    assert _TOKEN not in str(excinfo.value)


async def test_answer_callback_query_non_json_body_raises(http_recorder, fake_redis):
    http_recorder.responder = lambda request: httpx.Response(200, text="not json")
    with pytest.raises(ChannelDeliveryError, match="non-JSON body") as excinfo:
        await answer_callback_query("cb-1")
    assert _TOKEN not in str(excinfo.value)


async def test_answer_callback_query_ok_false_echoes_error_code_and_description(http_recorder, fake_redis):
    http_recorder.responder = lambda request: httpx.Response(
        200, json={"ok": False, "error_code": 400, "description": "query is too old"}
    )
    with pytest.raises(ChannelDeliveryError, match="error_code=400") as excinfo:
        await answer_callback_query("cb-1")
    assert "query is too old" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)


async def test_send_chat_action_non_200_json_error_surfaces_full_detail(http_recorder, fake_redis):
    # Telegram answers its structured error on the HTTP-error status too — the non-200
    # arm routes _error_detail, so error_code/description/parameters all surface.
    http_recorder.responder = lambda request: httpx.Response(
        429,
        json={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests: retry later",
            "parameters": {"retry_after": 5},
        },
    )
    with pytest.raises(ChannelDeliveryError, match="error_code=429") as excinfo:
        await send_chat_action(555, "typing")
    assert "description='Too Many Requests: retry later'" in str(excinfo.value)
    assert 'parameters={"retry_after":5}' in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)


async def test_send_chat_action_ok_false_surfaces_parameters(http_recorder, fake_redis):
    # The 200 ok:false arm routes the same helper: parameters (here migrate_to_chat_id) ride too.
    http_recorder.responder = lambda request: httpx.Response(
        200,
        json={
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: group migrated",
            "parameters": {"migrate_to_chat_id": -1001234567890},
        },
    )
    with pytest.raises(ChannelDeliveryError, match="error_code=400") as excinfo:
        await send_chat_action(555, "typing")
    assert 'parameters={"migrate_to_chat_id":-1001234567890}' in str(excinfo.value)


async def test_send_chat_action_non_200_non_json_body_falls_back_to_bounded_text(http_recorder, fake_redis):
    # A non-JSON error body (a proxy/WAF HTML page) falls back to the bounded raw text.
    http_recorder.responder = lambda request: httpx.Response(502, text="<html>" + "x" * 2000 + "</html>")
    with pytest.raises(ChannelDeliveryError, match="sendChatAction rejected") as excinfo:
        await send_chat_action(555, "typing")
    assert len(str(excinfo.value)) < 600  # detail bounded to 500 chars
    assert _TOKEN not in str(excinfo.value)


async def test_answer_callback_query_non_200_json_error_surfaces_full_detail(http_recorder, fake_redis):
    http_recorder.responder = lambda request: httpx.Response(
        400,
        json={"ok": False, "error_code": 400, "description": "query is too old", "parameters": {"retry_after": 1}},
    )
    with pytest.raises(ChannelDeliveryError, match="error_code=400") as excinfo:
        await answer_callback_query("cb-1")
    assert 'parameters={"retry_after":1}' in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)
