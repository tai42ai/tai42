"""An inbound reply carries the text of the message it quotes.

A participant who replies to an earlier message — WhatsApp's quote, a Telegram reply, a Slack
thread reply, a Twilio reply, a tapped button on the platform's own message, or an API message
naming an earlier one — reaches the tool turn with ``reply_to``: the quoted message's medium id,
whether the platform still holds it (``found``, ``not_found``, ``sensitive``) and, when found, its
text. The same quote reaches an asker: an async ask's continuation and a sync ask's answer object
carry it beside the answer, with the answer's params. The quote is part of the turn's record, so
forgetting the thread forgets it.

Every leg drives the live media-bridge stack over the vendor stubs; tool turns record what they
were started with through the ``e2e_record_and_reply`` probe, askers through the ask probes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any, cast
from urllib.parse import urlencode

import pytest
import redis

from tai42_e2e.channel_stubs import TINY_PNG, MediaBlob
from tai42_e2e.manifests import (
    BRIDGE_WHATSAPP_PHONE_ID,
    SLACK_ALLOWED_RECIPIENTS,
    SLACK_BOT_USER_ID,
    SLACK_DEFAULT_RECIPIENT,
    TELEGRAM_DEFAULT_RECIPIENT,
)
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_absent_async, wait_for_async
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import (
    SLACK_INBOUND_PATH,
    SLACK_INTERACTIVE_PATH,
    TELEGRAM_INBOUND_PATH,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    cancel_and_join,
    post_inbound,
    tool_text,
    wait_channel_send_count,
    wait_probe_entries,
    wait_probe_record,
    wait_twilio_send,
    wait_whatsapp_send,
)

pytestmark = [
    pytest.mark.needs(
        "kind:identity",
        "probe-tools",
        "setting:conversations",
        "setting:seeded-access-control",
        "store:redis",
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("telegram", "slack", "twilio", "whatsapp")),
        reason="the media-bridge vendor stubs are the mock leg; real legs run on the creds host",
    ),
]

# An https callback with no server behind it: the api-door legs wait inline, so it is never dialled.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"
# Every turn of the recording probe answers this prefix plus the message it was sent.
_REPLY_PREFIX = "re:"
# Far enough out that every async ask here is answered before it expires.
_PARK_SECONDS = 600.0


def _fresh_wa_id() -> str:
    return f"1650{uuid.uuid4().int % 10**9:09d}"


def _fresh_phone_id() -> str:
    return f"9{uuid.uuid4().int % 10**14:014d}"


def _fresh_chat_id() -> str:
    """A Telegram chat no recipient setting names, so every message from it bridges to a turn."""
    return str(800_000_000 + secrets.randbelow(10**8))


def _fresh_twilio_number() -> str:
    return f"+1555{secrets.randbelow(10**7):07d}"


def _quote_start(key: str) -> str:
    """The ``start_expr`` of the recording probe: whether the payload carries ``reply_to`` and its
    value, recorded under ``key``, and the reply ``re:<message>``."""
    value = '({has: has("reply_to"), reply_to: .reply_to} | tojson)'
    return f"{{key: {json.dumps(key)}, value: {value}, reply: ({json.dumps(_REPLY_PREFIX)} + .message)}}"


def _found(message_id: str, text: str) -> dict[str, Any]:
    return {"message_id": message_id, "status": "found", "text": text}


def _quoted(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The ``{has, reply_to}`` each recorded turn was started with, in turn order."""
    return [json.loads(entry["value"]) for entry in entries]


def _transcript_path(route_name: str, address: str) -> str:
    query = urlencode({"thread_id": f"bridge:{route_name}:{address}", "order": "asc"})
    return f"/api/conversations/{route_name}/transcript?{query}"


def _thread_delete_path(route_name: str, address: str) -> str:
    return f"/api/conversations/{route_name}/thread?{urlencode({'thread_id': f'bridge:{route_name}:{address}'})}"


@contextlib.asynccontextmanager
async def _tool_route(
    bridge: BridgeHarness,
    uniq: Callable[[str], str],
    *,
    channel: str,
    identity: str,
    tool: str,
    start: str,
    reply_expr: str | None = None,
) -> AsyncIterator[str]:
    """A channel route on ``(channel, identity)`` serving ``tool``, deleted on exit.

    An ask and a notify go from the channel's own sending identity, and only one route may hold
    a ``(channel, our_identity)`` pair, so each leg on that identity takes it in turn."""
    route_name = uniq("rt-route").replace("_", "-")
    execution_key = uniq("rt-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool=tool,
        execution_key=execution_key,
        channel=channel,
        our_identity=identity,
        start_expr=start,
        reply_expr=reply_expr,
    )
    try:
        yield route_name
    finally:
        await bridge.api().delete(f"/api/conversations/{route_name}")


async def _post(bridge: BridgeHarness, path: str, inbound: Any) -> None:
    response = await post_inbound(bridge.stack, path, inbound, port=bridge.stack.port_b)
    assert response.status_code in (200, 204), response.text


async def _whatsapp(
    bridge: BridgeHarness,
    identity: str,
    wa_id: str,
    text: str,
    *,
    wamid: str | None = None,
    context_id: str | None = None,
) -> None:
    inbound = bridge.whatsapp_inbound(
        phone_number_id=identity, wa_id=wa_id, text=text, wamid=wamid, context_id=context_id
    )
    await _post(bridge, WHATSAPP_INBOUND_PATH, inbound)


async def _call_tool(bridge: BridgeHarness, name: str, arguments: dict[str, Any]) -> Any:
    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        return await mcp.call_tool(name, arguments)


async def _park(bridge: BridgeHarness, question: str, **delivery: str) -> str:
    """Park an async ask of ``question`` (on ``delivery``'s channel and recipient) and return its id."""
    result = await _call_tool(
        bridge, "e2e_async_park_flow", {"question": question, "expiry_seconds": _PARK_SECONDS, **delivery}
    )
    return cast("dict[str, Any]", result.data)["interaction_id"]


async def _resumed(bridge: BridgeHarness, interaction_id: str) -> dict[str, Any]:
    """The one record the park's continuation left when the answer resumed it."""
    (entry,) = await wait_probe_record(bridge, f"async_resume:{interaction_id}", deadline=20.0)
    assert entry["branch"] == "answer", entry
    return entry


async def _sync_ask(bridge: BridgeHarness, arguments: dict[str, Any]) -> asyncio.Task[Any]:
    return asyncio.create_task(_call_tool(bridge, "e2e_ask_recorded", arguments))


async def _asked(bridge: BridgeHarness, task: asyncio.Task[Any], key: str) -> dict[str, Any]:
    """The answer object a sync ask got back, as the ask probe recorded it."""
    try:
        await asyncio.wait_for(task, timeout=20.0)
    finally:
        await cancel_and_join(task)
    (entry,) = await wait_probe_record(bridge, key)
    return entry


# -- WhatsApp -----------------------------------------------------------------------------------


@pytest.mark.needs("kind:channels:whatsapp", "helper:whatsapp")
async def test_a_whatsapp_quote_of_the_reply_and_of_the_first_message_is_found(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    identity, wa_id, key = _fresh_phone_id(), _fresh_wa_id(), uniq("rt-wa")
    first, second, third = uniq("rt-wa-1"), uniq("rt-wa-2"), uniq("rt-wa-3")
    first_wamid = f"wamid.{uuid.uuid4().hex}"
    async with _tool_route(
        bridge, uniq, channel="whatsapp", identity=identity, tool="e2e_record_and_reply", start=_quote_start(key)
    ) as route_name:
        await _whatsapp(bridge, identity, wa_id, first, wamid=first_wamid)
        reply = await wait_whatsapp_send(bridge.fake_whatsapp, _REPLY_PREFIX + first)
        await _whatsapp(bridge, identity, wa_id, second, context_id=reply["wamid"])
        await wait_whatsapp_send(bridge.fake_whatsapp, _REPLY_PREFIX + second)
        await _whatsapp(bridge, identity, wa_id, third, context_id=first_wamid)
        entries = await wait_probe_entries(bridge, key, 3)

        assert _quoted(entries) == [
            {"has": False, "reply_to": None},
            {"has": True, "reply_to": _found(reply["wamid"], _REPLY_PREFIX + first)},
            {"has": True, "reply_to": _found(first_wamid, first)},
        ]
        # The transcript read shows the quote on the quoting record.
        items = (await bridge.api().get(_transcript_path(route_name, wa_id)))["items"]
        assert [item.get("inbound_reply_to") for item in items] == [
            None,
            _found(reply["wamid"], _REPLY_PREFIX + first),
            _found(first_wamid, first),
        ]


@pytest.mark.needs("kind:channels:whatsapp", "helper:whatsapp")
async def test_a_whatsapp_quote_of_an_unknown_or_forgotten_message_is_not_found(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    identity, wa_id, key = _fresh_phone_id(), _fresh_wa_id(), uniq("rt-wa-nf")
    first = uniq("rt-wa-nf-1")
    never_sent = f"wamid.{uuid.uuid4().hex}"
    async with _tool_route(
        bridge, uniq, channel="whatsapp", identity=identity, tool="e2e_record_and_reply", start=_quote_start(key)
    ) as route_name:
        await _whatsapp(bridge, identity, wa_id, first)
        reply = await wait_whatsapp_send(bridge.fake_whatsapp, _REPLY_PREFIX + first)
        second = uniq("rt-wa-nf-2")
        await _whatsapp(bridge, identity, wa_id, second, context_id=never_sent)
        await wait_whatsapp_send(bridge.fake_whatsapp, _REPLY_PREFIX + second)

        await bridge.api().delete(_thread_delete_path(route_name, wa_id))
        await _whatsapp(bridge, identity, wa_id, uniq("rt-wa-nf-3"), context_id=reply["wamid"])
        entries = await wait_probe_entries(bridge, key, 3)

    not_found = {"status": "not_found", "text": None}
    assert _quoted(entries)[1:] == [
        {"has": True, "reply_to": {"message_id": never_sent, **not_found}},
        {"has": True, "reply_to": {"message_id": reply["wamid"], **not_found}},
    ]


@pytest.mark.needs("kind:channels:whatsapp", "helper:whatsapp")
async def test_a_whatsapp_quote_of_a_sensitive_answer_never_carries_its_text(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    wa_id, key = _fresh_wa_id(), uniq("rt-wa-sens")
    question, secret = uniq("rt-wa-sens-q"), uniq("rt-wa-sens-secret")
    answer_wamid = f"wamid.{uuid.uuid4().hex}"
    async with _tool_route(
        bridge,
        uniq,
        channel="whatsapp",
        identity=BRIDGE_WHATSAPP_PHONE_ID,
        tool="e2e_record_and_reply",
        start=_quote_start(key),
    ):
        task = asyncio.create_task(
            _call_tool(bridge, "e2e_sensitive_ask", {"question": question, "channel": "whatsapp", "recipient": wa_id})
        )
        try:
            await wait_whatsapp_send(bridge.fake_whatsapp, question)
            await _whatsapp(bridge, BRIDGE_WHATSAPP_PHONE_ID, wa_id, secret, wamid=answer_wamid)
            result = await asyncio.wait_for(task, timeout=20.0)
        finally:
            await cancel_and_join(task)
        assert result.data == secret

        await _whatsapp(bridge, BRIDGE_WHATSAPP_PHONE_ID, wa_id, uniq("rt-wa-sens-q2"), context_id=answer_wamid)
        entries = await wait_probe_entries(bridge, key, 1)

    assert _quoted(entries) == [
        {"has": True, "reply_to": {"message_id": answer_wamid, "status": "sensitive", "text": None}}
    ]
    assert secret not in json.dumps(entries)


@pytest.mark.needs("kind:channels:whatsapp", "kind:interactions", "helper:whatsapp")
async def test_an_ask_answered_on_whatsapp_quoting_the_assistant_hands_the_asker_the_quote(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    wa_id, key, ask_key = _fresh_wa_id(), uniq("rt-wa-ask"), uniq("rt-wa-ask-sync")
    hello = uniq("rt-wa-ask-hello")
    async with _tool_route(
        bridge,
        uniq,
        channel="whatsapp",
        identity=BRIDGE_WHATSAPP_PHONE_ID,
        tool="e2e_record_and_reply",
        start=_quote_start(key),
    ):
        await _whatsapp(bridge, BRIDGE_WHATSAPP_PHONE_ID, wa_id, hello)
        reply = await wait_whatsapp_send(bridge.fake_whatsapp, _REPLY_PREFIX + hello)
        quote = _found(reply["wamid"], _REPLY_PREFIX + hello)

        # An async ask: its continuation runs with the answer and the quote.
        parked_question, parked_answer = uniq("rt-wa-ask-q1"), uniq("rt-wa-ask-a1")
        interaction_id = await _park(bridge, parked_question, channel="whatsapp", recipient=wa_id)
        await wait_whatsapp_send(bridge.fake_whatsapp, parked_question)
        await _whatsapp(bridge, BRIDGE_WHATSAPP_PHONE_ID, wa_id, parked_answer, context_id=reply["wamid"])
        resumed = await _resumed(bridge, interaction_id)
        assert json.loads(resumed["value"]) == parked_answer
        assert resumed["reply_to"] == quote

        # A sync ask: the answer object it gets back holds the same.
        sync_question, sync_answer = uniq("rt-wa-ask-q2"), uniq("rt-wa-ask-a2")
        task = await _sync_ask(
            bridge, {"key": ask_key, "question": sync_question, "channel": "whatsapp", "recipient": wa_id}
        )
        await wait_whatsapp_send(bridge.fake_whatsapp, sync_question)
        await _whatsapp(bridge, BRIDGE_WHATSAPP_PHONE_ID, wa_id, sync_answer, context_id=reply["wamid"])
        asked = await _asked(bridge, task, ask_key)
    assert (asked["answer"], asked["reply_to"]) == (sync_answer, quote)


# -- Telegram, Slack, Twilio ------------------------------------------------------------------


@pytest.mark.needs("kind:channels:telegram", "helper:telegram")
async def test_a_telegram_reply_to_the_bot_is_found(media_bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    bridge = media_bridge
    chat, key = _fresh_chat_id(), uniq("rt-tg")
    first, second = uniq("rt-tg-1"), uniq("rt-tg-2")
    async with _tool_route(
        bridge,
        uniq,
        channel="telegram",
        identity=bridge.telegram_our_identity,
        tool="e2e_record_and_reply",
        start=_quote_start(key),
    ):
        await _post(bridge, TELEGRAM_INBOUND_PATH, bridge.telegram_inbound_text(chat_id=chat, text=first))
        (reply,) = await wait_channel_send_count(bridge.telegram, _REPLY_PREFIX + first, 1)
        inbound = bridge.telegram_inbound_reply(chat_id=chat, reply_to_message_id=reply["message_id"], text=second)
        await _post(bridge, TELEGRAM_INBOUND_PATH, inbound)
        entries = await wait_probe_entries(bridge, key, 2)

    assert _quoted(entries)[1] == {"has": True, "reply_to": _found(str(reply["message_id"]), _REPLY_PREFIX + first)}


@pytest.mark.needs("kind:channels:slack", "helper:slack", "setting:router:notifications", "setting:tool:notify_user")
async def test_a_slack_thread_reply_under_the_bot_message_is_found(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    channel, key, notice = SLACK_ALLOWED_RECIPIENTS[0], uniq("rt-slack"), uniq("rt-slack-notice")
    async with _tool_route(
        bridge, uniq, channel="slack", identity=SLACK_BOT_USER_ID, tool="e2e_record_and_reply", start=_quote_start(key)
    ):
        result = await _call_tool(bridge, "notify_user", {"message": notice, "channel": "slack", "recipient": channel})
        assert "notification sent via 'slack'" in tool_text(result)
        (posted,) = await wait_channel_send_count(bridge.slack, notice, 1)
        inbound = bridge.slack_inbound_text(
            channel=channel, thread_ts=posted["ts"], text=uniq("rt-slack-reply"), event_id=f"Ev{uniq('rt-slack-ev')}"
        )
        await _post(bridge, SLACK_INBOUND_PATH, inbound)
        entries = await wait_probe_entries(bridge, key, 1)

    assert _quoted(entries) == [{"has": True, "reply_to": _found(posted["ts"], notice)}]


@pytest.mark.needs("kind:channels:twilio", "helper:twilio")
async def test_a_twilio_reply_naming_our_message_is_found_and_a_plain_text_quotes_nothing(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    identity, client, key = _fresh_twilio_number(), _fresh_twilio_number(), uniq("rt-tw")
    first = uniq("rt-tw-1")

    async def message(text: str, original_replied_sid: str | None = None) -> None:
        inbound = bridge.twilio_inbound(
            our_identity=identity,
            client=client,
            text=text,
            port=bridge.stack.port_b,
            original_replied_sid=original_replied_sid,
        )
        await _post(bridge, TWILIO_INBOUND_PATH, inbound)

    async with _tool_route(
        bridge, uniq, channel="twilio", identity=identity, tool="e2e_record_and_reply", start=_quote_start(key)
    ):
        await message(first)
        reply = await wait_twilio_send(bridge.fake_twilio, _REPLY_PREFIX + first)
        second = uniq("rt-tw-2")
        await message(second, original_replied_sid=reply["sid"])
        await wait_twilio_send(bridge.fake_twilio, _REPLY_PREFIX + second)
        await message(uniq("rt-tw-3"))
        entries = await wait_probe_entries(bridge, key, 3)

    assert _quoted(entries)[1:] == [
        {"has": True, "reply_to": _found(reply["sid"], _REPLY_PREFIX + first)},
        {"has": False, "reply_to": None},
    ]


# -- a tapped button -------------------------------------------------------------------------


@pytest.mark.needs("kind:channels:telegram", "helper:telegram")
async def test_a_telegram_button_tap_hands_the_asker_the_tapped_message(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    key, question = uniq("rt-tg-tap"), uniq("rt-tg-tap-q")
    options = [uniq("rt-tg-tap-a"), uniq("rt-tg-tap-b")]
    async with _tool_route(
        bridge,
        uniq,
        channel="telegram",
        identity=bridge.telegram_our_identity,
        tool="e2e_record_and_reply",
        start=_quote_start(uniq("rt-tg-tap-turn")),
    ):
        task = await _sync_ask(
            bridge,
            {"key": key, "question": question, "channel": "telegram", "answer_format": "select", "options": options},
        )
        (sent,) = await wait_channel_send_count(bridge.telegram, question, 1)
        rows = sent["reply_markup"]["inline_keyboard"]
        (data,) = [row[0]["callback_data"] for row in rows if row[0]["text"] == options[1]]
        tap = bridge.telegram_callback_query(
            chat_id=TELEGRAM_DEFAULT_RECIPIENT, message_id=sent["message_id"], data=data
        )
        await _post(bridge, TELEGRAM_INBOUND_PATH, tap)
        asked = await _asked(bridge, task, key)

    assert asked["answer"] == options[1]
    reply_to = asked["reply_to"]
    assert (reply_to["message_id"], reply_to["status"]) == (str(sent["message_id"]), "found")
    assert question in reply_to["text"]


@pytest.mark.needs("kind:channels:slack", "helper:slack")
async def test_a_slack_button_tap_hands_the_asker_the_tapped_message(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    key, question = uniq("rt-slack-tap"), uniq("rt-slack-tap-q")
    options = [uniq("rt-slack-tap-a"), uniq("rt-slack-tap-b")]
    async with _tool_route(
        bridge,
        uniq,
        channel="slack",
        identity=SLACK_BOT_USER_ID,
        tool="e2e_record_and_reply",
        start=_quote_start(uniq("rt-slack-tap-turn")),
    ):
        task = await _sync_ask(
            bridge,
            {"key": key, "question": question, "channel": "slack", "answer_format": "select", "options": options},
        )
        (posted,) = await wait_channel_send_count(bridge.slack, question, 1)
        buttons = [element for block in posted["blocks"] if block["type"] == "actions" for element in block["elements"]]
        (button,) = [element for element in buttons if element["value"] == options[1]]
        payload = {
            "type": "block_actions",
            "user": {"id": "U0PARTICIPANT"},
            "container": {"type": "message", "message_ts": posted["ts"], "channel_id": SLACK_DEFAULT_RECIPIENT},
            "channel": {"id": SLACK_DEFAULT_RECIPIENT},
            "actions": [
                {"action_id": button["action_id"], "type": "button", "action_ts": "1700.1", "value": button["value"]}
            ],
        }
        await _post(bridge, SLACK_INTERACTIVE_PATH, bridge.slack_interactive(payload=payload))
        asked = await _asked(bridge, task, key)

    assert asked["answer"] == options[1]
    reply_to = asked["reply_to"]
    assert (reply_to["message_id"], reply_to["status"]) == (posted["ts"], "found")
    assert question in reply_to["text"]


# -- the API door ---------------------------------------------------------------------------


async def test_an_api_message_naming_an_earlier_message_carries_its_answer(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    key = uniq("rt-api")
    exec_key = uniq("rt-api-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("rt-api-route").replace("_", "-")
    await bridge.create_tool_api_route(
        route_name=route_name,
        tool="e2e_record_and_reply",
        execution_key=exec_key,
        callback_url=_UNREACHABLE_CALLBACK,
        start_expr=_quote_start(key),
    )
    caller = await bridge.mint_key(user_id=uniq("rt-api-caller"), scopes=["e2e-all"])
    api = bridge.api(token=caller)
    path = f"/api/conversations/{route_name}/messages"
    user, first = uniq("rt-api-user"), uniq("rt-api-1")

    sent = await api.post(path, json={"external_user_id": user, "text": first, "wait_seconds": 20}, expect=200)
    assert sent["answer"]["answer"] == _REPLY_PREFIX + first
    body = {"external_user_id": user, "text": uniq("rt-api-2"), "reply_to_message_id": sent["message_id"]}
    await api.post(path, json={**body, "wait_seconds": 20}, expect=200)

    malformed = await api.request_raw("POST", path, json={**body, "reply_to_message_id": "not-a-message-id"})
    assert malformed.status_code == 400, malformed.text

    other = {**body, "external_user_id": uniq("rt-api-other"), "wait_seconds": 20}
    await api.post(path, json=other, expect=200)
    entries = await wait_probe_entries(bridge, key, 3)

    assert _quoted(entries)[1:] == [
        {"has": True, "reply_to": _found(sent["message_id"], _REPLY_PREFIX + first)},
        {"has": True, "reply_to": {"message_id": sent["message_id"], "status": "not_found", "text": None}},
    ]


# -- forgetting the thread ------------------------------------------------------------------


def _state_hashes_carrying(bridge: BridgeHarness, interaction_ids: list[str], needle: str) -> list[str]:
    """The interaction state hashes of ``interaction_ids`` whose fields or values carry ``needle``."""
    client = redis.Redis.from_url(bridge.stack.resources.redis_url, decode_responses=True)
    try:
        carrying = []
        for interaction_id in interaction_ids:
            for key in client.scan_iter(match=f"*state:{interaction_id}", count=500):
                fields = cast("dict[str, str]", client.hgetall(key))
                if any(needle in item for pair in fields.items() for item in pair):
                    carrying.append(key)
        return carrying
    finally:
        client.close()


@pytest.mark.needs("kind:channels:whatsapp", "kind:interactions", "helper:whatsapp")
async def test_forgetting_the_thread_forgets_the_quote_and_withdraws_its_pending_ask(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    identity, wa_id = _fresh_phone_id(), _fresh_wa_id()
    first_question, second_question = uniq("rt-forget-q1"), uniq("rt-forget-q2")
    first, first_wamid = uniq("rt-forget-1"), f"wamid.{uuid.uuid4().hex}"
    # Each turn parks an inbox ask in the thread; the quoting turn asks the second question.
    start = f'{{marker: (if has("reply_to") then {json.dumps(second_question)} else {json.dumps(first_question)} end)}}'
    async with _tool_route(
        bridge,
        uniq,
        channel="whatsapp",
        identity=identity,
        tool="e2e_tool_target_park",
        start=start,
        reply_expr=".result.answer",
    ) as route_name:
        await _whatsapp(bridge, identity, wa_id, first, wamid=first_wamid)
        (first_park,) = await wait_probe_record(bridge, f"tool_target_park:{first_question}", deadline=20.0)
        await _whatsapp(bridge, identity, wa_id, uniq("rt-forget-2"), context_id=first_wamid)
        (second_park,) = await wait_probe_record(bridge, f"tool_target_park:{second_question}", deadline=20.0)
        parks = [first_park["interaction_id"], second_park["interaction_id"]]

        items = (await bridge.api().get(_transcript_path(route_name, wa_id)))["items"]
        assert items[1]["inbound_reply_to"] == _found(first_wamid, first)
        assert _state_hashes_carrying(bridge, parks, first) == []

        await bridge.api().delete(_thread_delete_path(route_name, wa_id))

        await bridge.api().get(_transcript_path(route_name, wa_id), expect=404)

        async def withdrawn() -> bool | None:
            listing = await bridge.api().get("/api/interactions?page=1&pageSize=200")
            pending = {item["interaction_id"] for item in listing["items"]}
            return True if pending.isdisjoint(parks) else None

        await wait_for_async(withdrawn, deadline=20.0, message="a forgotten thread's ask stayed pending")

        async def delivered() -> list[str] | None:
            fired = [park for park in parks if bridge.stack.records(f"tool_target_deliver:{park}")]
            return fired or None

        await wait_absent_async(delivered, window=5.0, interval=0.5, message="a withdrawn ask ran its continuation")
        assert _state_hashes_carrying(bridge, parks, first) == []


# -- the answer's params --------------------------------------------------------------------


@pytest.mark.needs("kind:channels:telegram", "kind:interactions", "kind:storage", "helper:telegram")
async def test_a_telegram_photo_answer_hands_the_asker_its_media_params(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    key = uniq("rt-tg-photo")

    async def photo_reply(question: str, caption: str) -> int:
        """Answer the ask of ``question`` with a photo replying to it; return the question's message id."""
        (sent,) = await wait_channel_send_count(bridge.telegram, question, 1)
        file_id = uniq("rt-tg-photo-file")
        bridge.telegram.media[file_id] = MediaBlob(body=TINY_PNG, content_type="image/png")
        inbound = bridge.telegram_inbound_photo(
            chat_id=TELEGRAM_DEFAULT_RECIPIENT, file_id=file_id, caption=caption, reply_to_message_id=sent["message_id"]
        )
        await _post(bridge, TELEGRAM_INBOUND_PATH, inbound)
        return sent["message_id"]

    def assert_media_params(params: dict[str, str] | None) -> None:
        assert params is not None
        assert (params["media_kind"], params["media_mime_type"]) == ("image", "image/png"), params
        assert params["media_id"], params

    async with _tool_route(
        bridge,
        uniq,
        channel="telegram",
        identity=bridge.telegram_our_identity,
        tool="e2e_record_and_reply",
        start=_quote_start(uniq("rt-tg-photo-turn")),
    ):
        parked_question, parked_caption = uniq("rt-tg-photo-q1"), uniq("rt-tg-photo-c1")
        interaction_id = await _park(bridge, parked_question, channel="telegram")
        message_id = await photo_reply(parked_question, parked_caption)
        resumed = await _resumed(bridge, interaction_id)
        assert json.loads(resumed["value"]) == parked_caption
        assert (resumed["reply_to"]["message_id"], resumed["reply_to"]["status"]) == (str(message_id), "found")
        assert_media_params(resumed["params"])

        sync_question, sync_caption = uniq("rt-tg-photo-q2"), uniq("rt-tg-photo-c2")
        task = await _sync_ask(bridge, {"key": key, "question": sync_question, "channel": "telegram"})
        await photo_reply(sync_question, sync_caption)
        asked = await _asked(bridge, task, key)
    assert asked["answer"] == sync_caption
    assert_media_params(asked["params"])


@pytest.mark.needs("kind:channels:web", "kind:interactions")
async def test_a_web_answer_hands_the_asker_the_session_link_params(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    identity, key, link_value = uniq("rt-web-site").replace("_", "-"), uniq("rt-web"), uniq("rt-web-x")
    async with _tool_route(
        bridge,
        uniq,
        channel="web",
        identity=identity,
        tool="e2e_record_and_reply",
        start=_quote_start(uniq("rt-web-turn")),
    ):
        web, page = await WebChatClient.open_page(
            bridge.stack.origin(bridge.stack.port_b),
            identity,
            store_url=bridge.stack.resources.redis_url,
            query={"x": link_value},
        )
        assert page.status_code == 200, page.text

        parked_question, parked_answer = uniq("rt-web-q1"), uniq("rt-web-a1")
        interaction_id = await _park(bridge, parked_question, channel="web", recipient=web.recipient)
        assert (await web.answer(interaction_id, parked_answer)).status_code == 200, "the web answer was refused"
        resumed = await _resumed(bridge, interaction_id)
        assert json.loads(resumed["value"]) == parked_answer
        assert (resumed["reply_to"], resumed["params"]) == (None, {"x": link_value})

        sync_question, sync_answer = uniq("rt-web-q2"), uniq("rt-web-a2")
        task = await _sync_ask(
            bridge, {"key": key, "question": sync_question, "channel": "web", "recipient": web.recipient}
        )
        frames = await web.frames(
            until=lambda event, data: event == "chat.question" and data["question"] == sync_question
        )
        (asking,) = [data for event, data in frames if event == "chat.question" and data["question"] == sync_question]
        assert (await web.answer(asking["interaction_id"], sync_answer)).status_code == 200
        asked = await _asked(bridge, task, key)
    assert (asked["answer"], asked["reply_to"], asked["params"]) == (sync_answer, None, {"x": link_value})


# -- elicitation ----------------------------------------------------------------------------


@pytest.mark.needs("kind:channels:whatsapp", "kind:interactions", "helper:whatsapp")
async def test_an_elicitation_answered_in_the_inbox_is_typed_and_quotes_nothing(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    identity, wa_id, key, question = _fresh_phone_id(), _fresh_wa_id(), uniq("rt-elicit"), uniq("rt-elicit-q")
    start = f"{{key: {json.dumps(key)}, question: {json.dumps(question)}}}"
    async with _tool_route(bridge, uniq, channel="whatsapp", identity=identity, tool="e2e_elicit_record", start=start):
        await _whatsapp(bridge, identity, wa_id, uniq("rt-elicit-msg"))

        async def pending() -> str | None:
            listing = await bridge.api().get("/api/interactions?page=1&pageSize=200")
            return next((item["interaction_id"] for item in listing["items"] if question in item["question"]), None)

        interaction_id = await wait_for_async(pending, deadline=20.0, message="the elicitation never reached the inbox")
        answer = {"colour": uniq("rt-elicit-colour"), "count": 3}
        await bridge.api().post(f"/api/interactions/{interaction_id}/answer", json={"answer": answer})
        (entry,) = await wait_probe_record(bridge, key, deadline=20.0)

    assert (entry["data"], entry["typed"], entry["reply_to"]) == (answer, True, None)
