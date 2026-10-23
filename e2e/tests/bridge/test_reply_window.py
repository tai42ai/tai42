"""The ``reply_window`` a turn carries — whether the channel lets a free-form reply reach the client now.

Every turn's ``turn`` block carries ``reply_window: {state, closes_at}``, read once per turn from the
channel the turn replies on:

* WhatsApp Cloud opens the pair's 24-hour window at the client message's own Meta ``timestamp``, so a
  fresh message reads ``open`` with ``closes_at`` = timestamp + 24h, and a message older than 24 hours
  opens nothing (``closed``). An event turn on the same thread reads the same pair's window.
* Twilio opens the window for a ``whatsapp:`` pair at receive time; an SMS pair has no window
  (``none``).
* A channel with no window (web) and the api door (no channel) read ``none``.
* An agent route reads the window in its ``start_expr`` (``.turn.reply_window``) and the agent run
  receives it.

The tool routes run ``e2e_record`` with ``value`` = the turn's ``reply_window`` as JSON, so the
recorded side effect is exactly what the route's jq saw.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import (
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    request_mentions,
    script_reply,
    wait_probe_entries,
    wait_probe_record,
    wait_whatsapp_send,
)

_DAY = timedelta(hours=24)

pytestmark = [
    pytest.mark.needs("kind:identity", "probe-tools", "setting:conversations", "setting:seeded-access-control"),
    pytest.mark.skipif(
        HarnessSettings().is_real("whatsapp")
        or HarnessSettings().is_real("twilio")
        or HarnessSettings().is_real("llm"),
        reason="FakeWhatsApp + FakeTwilio + scripted-LLM is the mock leg; real legs on the creds host",
    ),
]


def _record_start_expr(probe: str) -> str:
    """A tool route ``start_expr`` that records the turn's ``reply_window`` under ``probe``."""
    return f'{{key: "{probe}", value: (.turn.reply_window | tojson)}}'


async def _record_route(
    bridge: BridgeHarness, uniq: Callable[[str], str], tag: str, *, channel: str, our_identity: str
) -> tuple[str, str]:
    """A silent ``e2e_record`` tool route on ``(channel, our_identity)``; returns ``(route name, probe)``."""
    exec_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq(f"{tag}-route").replace("_", "-")
    probe = uniq(f"{tag}-probe")
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_record",
        execution_key=exec_key,
        channel=channel,
        our_identity=our_identity,
        start_expr=_record_start_expr(probe),
        reply_expr="null",
    )
    return route_name, probe


def _window(entry: dict) -> dict:
    return json.loads(entry["value"])


def _open_until(seconds: int) -> dict:
    """The ``open`` window that closes 24 hours after the epoch-seconds ``seconds``, as the turn carries it."""
    closes_at = datetime.fromtimestamp(seconds, UTC) + _DAY
    return {"state": "open", "closes_at": closes_at.isoformat().replace("+00:00", "Z")}


def _fresh_wa_id() -> str:
    return f"1650{uuid.uuid4().int % 10**9:09d}"


def _fresh_phone_id() -> str:
    return f"9{uuid.uuid4().int % 10**14:014d}"


def _fresh_number() -> str:
    return f"+1650{uuid.uuid4().int % 10**7:07d}"


@pytest.mark.needs("kind:channels:whatsapp", "helper:whatsapp", "store:redis")
async def test_whatsapp_message_opens_the_window_from_its_timestamp_and_an_event_turn_reads_it(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = _fresh_phone_id()
    wa_id = _fresh_wa_id()
    route_name, probe = await _record_route(bridge, uniq, "rwwa", channel="whatsapp", our_identity=identity)

    sent_at = int(time.time()) - 60
    inbound = bridge.whatsapp_inbound(phone_number_id=identity, wa_id=wa_id, text="hello", timestamp=sent_at)
    resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound, port=bridge.stack.port_b)
    assert resp.status_code == 200, resp.text
    (first,) = await wait_probe_record(bridge, probe, deadline=20.0)
    assert _window(first) == _open_until(sent_at)

    # An event on the same thread runs as a turn on the same pair, so it reads the same window.
    await bridge.api().post(
        f"/api/conversations/{route_name}/events",
        json={"address": wa_id, "event": {"event_id": uniq("rwwa-evt"), "kind": "status.update", "payload": {}}},
        expect=202,
    )
    entries = await wait_probe_entries(bridge, probe, 2)
    assert _window(entries[1]) == _open_until(sent_at)


@pytest.mark.needs("kind:channels:whatsapp", "helper:whatsapp", "store:redis")
async def test_whatsapp_message_older_than_a_day_leaves_the_window_closed(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = _fresh_phone_id()
    _route_name, probe = await _record_route(bridge, uniq, "rwold", channel="whatsapp", our_identity=identity)

    sent_at = int(time.time()) - int((_DAY + timedelta(hours=1)).total_seconds())
    inbound = bridge.whatsapp_inbound(phone_number_id=identity, wa_id=_fresh_wa_id(), text="late", timestamp=sent_at)
    resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound, port=bridge.stack.port_b)
    assert resp.status_code == 200, resp.text
    (entry,) = await wait_probe_record(bridge, probe, deadline=20.0)
    assert _window(entry) == {"state": "closed", "closes_at": None}


@pytest.mark.needs("kind:channels:twilio", "helper:twilio", "store:redis")
async def test_twilio_whatsapp_pair_is_open_and_an_sms_pair_has_no_window(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    whatsapp_identity = f"whatsapp:{_fresh_number()}"
    _wa_route, wa_probe = await _record_route(bridge, uniq, "rwtw", channel="twilio", our_identity=whatsapp_identity)
    before = datetime.now(UTC)
    inbound = bridge.twilio_inbound(our_identity=whatsapp_identity, client=f"whatsapp:{_fresh_number()}", text="hi")
    resp = await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=bridge.stack.port_b)
    assert resp.status_code == 204, resp.text
    (entry,) = await wait_probe_record(bridge, wa_probe, deadline=20.0)
    after = datetime.now(UTC)
    window = _window(entry)
    assert window["state"] == "open"
    # Twilio sends no message time, so the window opens at receive time; the close time is whole
    # seconds, the form jq's ``fromdateiso8601`` reads.
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", window["closes_at"]), window["closes_at"]
    closes_at = datetime.fromisoformat(window["closes_at"])
    assert before + _DAY - timedelta(seconds=1) <= closes_at <= after + _DAY

    sms_identity = _fresh_number()
    _sms_route, sms_probe = await _record_route(bridge, uniq, "rwsms", channel="twilio", our_identity=sms_identity)
    inbound = bridge.twilio_inbound(our_identity=sms_identity, client=_fresh_number(), text="hi")
    resp = await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=bridge.stack.port_b)
    assert resp.status_code == 204, resp.text
    (entry,) = await wait_probe_record(bridge, sms_probe, deadline=20.0)
    assert _window(entry) == {"state": "none", "closes_at": None}


@pytest.mark.needs("kind:channels:web", "store:redis")
async def test_web_channel_and_api_door_turns_have_no_window(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    identity = uniq("rwweb-site").replace("_", "-")
    _web_route, web_probe = await _record_route(bridge, uniq, "rwweb", channel="web", our_identity=identity)
    web, page = await WebChatClient.open_page(
        bridge.stack.origin(bridge.stack.port_b), identity, store_url=bridge.stack.resources.redis_url
    )
    assert page.status_code == 200, page.text
    sent = await web.send("hello")
    assert sent.status_code == 200, sent.text
    (entry,) = await wait_probe_record(bridge, web_probe, deadline=20.0)
    assert _window(entry) == {"state": "none", "closes_at": None}

    exec_key = uniq("rwapi-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    api_route = uniq("rwapi-route").replace("_", "-")
    api_probe = uniq("rwapi-probe")
    await bridge.api().post(
        f"/api/conversations/{api_route}",
        json={
            "door": "api",
            "target_kind": "tool",
            "target_name": "e2e_record",
            "execution_key": exec_key,
            "start_expr": {"content": _record_start_expr(api_probe)},
        },
    )
    caller_token = await bridge.mint_key(user_id=uniq("rwapi-caller"), scopes=["e2e-all"])
    await bridge.api(token=caller_token).post(
        f"/api/conversations/{api_route}/messages",
        json={"external_user_id": uniq("rwapi-user"), "text": "hello"},
        expect=202,
    )
    (entry,) = await wait_probe_record(bridge, api_probe, deadline=20.0)
    assert _window(entry) == {"state": "none", "closes_at": None}


@pytest.mark.needs("kind:channels:whatsapp", "helper:llm", "helper:whatsapp", "setting:agent")
async def test_agent_route_start_expr_hands_the_window_to_the_agent_run(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = _fresh_phone_id()
    wa_id = _fresh_wa_id()
    exec_key = uniq("rwagent-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=uniq("rwagent-route").replace("_", "-"),
        agent="tools_agent",
        execution_key=exec_key,
        channel="whatsapp",
        our_identity=identity,
        start_expr=(
            '{user_message: {content: ("window " + .turn.reply_window.state'
            ' + " until " + (.turn.reply_window.closes_at // "never"))}}'
        ),
    )
    answer = uniq("rwagent-ans")
    script_reply(bridge.llm_stub, answer)

    sent_at = int(time.time()) - 60
    inbound = bridge.whatsapp_inbound(phone_number_id=identity, wa_id=wa_id, text="hello", timestamp=sent_at)
    resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound, port=bridge.stack.port_b)
    assert resp.status_code == 200, resp.text
    await wait_whatsapp_send(bridge.fake_whatsapp, answer)

    # The agent's own LLM request carries the message the start_expr built from the window.
    expected = _open_until(sent_at)
    assert request_mentions(bridge.llm_stub, 0, f"window open until {expected['closes_at']}"), bridge.llm_stub.requests
