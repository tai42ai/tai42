"""L27 — ask-less form notifications, end to end over both delivery surfaces.

``notify_user(schema=...)`` sends a fillable form with NO pending ask: the guest's
submission enters the conversation as an ordinary structured guest message — rendered
``label: value`` text every consumer sees, plus the structured values under the tool
payload's ``form`` key — never as an answer to anything.

The web leg drives the channel's own public doors (the SSE stream carries the ONE
``chat.form`` card; ``POST /forms/{token}`` is the submission door): resubmission is a
second guest turn, a foreign or unknown token is ONE uniform 404, and the door never
validates the values against the schema — schema-violating values bridge verbatim (the
pinned no-validation transport contract). The whatsapp legs drive the Flow rendering on
the FakeWhatsApp stub: the flow token rides the ``tai42-nf:`` namespace with NO pending
reservation, a signed ``nfm_reply`` carrying it bridges the schema-coerced dict, a reply
on a pair with a LIVE ask never touches that ask (the masquerade fence), and a schema
sidecar miss degrades the reply to raw string values — accepted, never dropped. The
capability leg pins the 501 refusal (a channel without the flag) leaving no feed entry.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any, cast

import httpx
import pytest
import redis as redis_lib

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
    BRIDGE_WHATSAPP_WABA_ID,
)
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import (
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    cancel_and_join,
    post_inbound,
    script_reply,
    wait_probe_record,
    wait_send_to,
    wait_whatsapp_send,
)

# The scripted-LLM + FakeWhatsApp legs step aside when either seam is selected real,
# exactly as the l7 module does; the web + capability legs have no whatsapp/llm seam
# and run everywhere.
_whatsapp_mock_leg = pytest.mark.skipif(
    HarnessSettings().is_real("whatsapp") or HarnessSettings().is_real("llm"),
    reason="FakeWhatsApp + scripted-LLM is the 'whatsapp'/'llm' mock leg; real legs on the creds host",
)

# The payload map a form-recording tool route uses: keyed by the submission's own
# ``topic`` value (each leg sends a unique marker there), recording the rendered
# message text AND the structured form (serialized — ``e2e_record`` takes a string).
_FORM_PAYLOAD_EXPR = "{key: .form.topic, value: ({message: .message, form: .form} | tojson)}"

# One generic answer schema per surface (client-agnostic field names). ``count`` /
# ``amount`` are integers: a WhatsApp Flow returns every input as a STRING, so the
# integer property is what proves (or, on a sidecar miss, disproves) the coercion.
_WEB_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "title": "Topic"},
        "count": {"type": "integer", "title": "Count"},
    },
    "required": ["topic", "count"],
}


def _whatsapp_schema(leg: str) -> dict[str, Any]:
    """A generic answer schema whose canonical hash is unique to ``leg``.

    The whatsapp plugin caches the published-flow id AND the schema sidecar under the
    schema's hash in the stack redis with no TTL (and no per-test reset), so two legs
    sharing one schema would share those entries — the created-once Flow census and the
    sidecar-miss degradation would then depend on execution order. The root
    ``description`` discriminator keeps each leg's cache entries its own without
    touching the field mapping or the rendered labels."""
    return {
        "type": "object",
        "description": f"{leg} answer form",
        "properties": {
            "topic": {"type": "string", "title": "Topic"},
            "amount": {"type": "integer", "title": "Amount"},
        },
        "required": ["topic", "amount"],
    }


def _base_url(bridge: BridgeHarness) -> str:
    return f"http://{bridge.stack.host}:{bridge.stack.port_b}"


def _fresh_wa_id() -> str:
    """A distinct human wa_id per leg (the l7 convention), so no two legs contend for
    the single pending-question slot on one ``(phone_number_id, wa_id)`` pair."""
    return f"1650{uuid.uuid4().int % 10**9:09d}"


def _fresh_phone_id() -> str:
    """A distinct routed ``phone_number_id`` per leg (a route's ``our_identity``)."""
    return f"9{uuid.uuid4().int % 10**14:014d}"


async def _form_record_route(bridge: BridgeHarness, uniq: Callable[[str], str], *, channel: str, identity: str) -> None:
    """A ``target_kind=tool`` route on ``(channel, identity)`` whose tool records each
    delivered turn under the submission's ``topic`` value — message text and structured
    form together. The reply maps to null so the turn is silent (nothing re-enters the
    conversation)."""
    exec_key = uniq("l27-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=uniq("l27-route").replace("_", "-"),
        tool="e2e_record",
        execution_key=exec_key,
        channel=channel,
        our_identity=identity,
        payload_expr=_FORM_PAYLOAD_EXPR,
        reply_expr="null",
    )


async def _recorded_form_turn(bridge: BridgeHarness, topic_marker: str) -> dict[str, Any]:
    """The ONE turn the recording tool captured for the submission whose ``topic`` was
    ``topic_marker``, decoded to its ``{"message", "form"}`` payload."""
    entries = await wait_probe_record(bridge, topic_marker, deadline=20.0)
    assert len(entries) == 1, f"expected exactly one recorded turn for {topic_marker!r}, saw {len(entries)}"
    decoded = json.loads(entries[0]["value"])
    assert isinstance(decoded, dict), decoded
    return decoded


async def _post_form(
    bridge: BridgeHarness, token: str, values: dict[str, Any], *, cookies: dict[str, str]
) -> httpx.Response:
    """POST one submission to the web form door as the session ``cookies`` presents."""
    async with httpx.AsyncClient(timeout=15.0, cookies=cookies) as client:
        return await client.post(f"{_base_url(bridge)}/api/channels/web/forms/{token}", json={"values": values})


def _stack_redis_get(bridge: BridgeHarness, key: str) -> str | None:
    """One value out of the stack's shared Redis — the store peek a spec uses when the
    fact it needs (a reservation, a sidecar entry) is deliberately absent from every door."""
    client = redis_lib.Redis.from_url(bridge.stack.resources.redis_url, decode_responses=True)
    try:
        # redis-py types every command as the sync/async union; this client is sync.
        return cast(str | None, client.get(key))
    finally:
        client.close()


def _stack_redis_delete(bridge: BridgeHarness, key: str) -> int:
    client = redis_lib.Redis.from_url(bridge.stack.resources.redis_url, decode_responses=True)
    try:
        return cast(int, client.delete(key))
    finally:
        client.close()


def _whatsapp_pending_key(phone_number_id: str, wa_id: str) -> str:
    """The whatsapp plugin's own pending-ask reservation key for a pair."""
    return f"channel:whatsapp:pending:{phone_number_id}:{wa_id}"


def _whatsapp_schema_sidecar_key(schema_hash: str) -> str:
    """The whatsapp plugin's durable schema sidecar key beside the published-flow id."""
    return f"channel:whatsapp:flow-schema:{BRIDGE_WHATSAPP_WABA_ID}:{schema_hash}"


async def test_web_form_card_submit_resubmit_foreign_404_and_no_schema_validation(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = uniq("l27-site").replace("_", "-")
    await _form_record_route(bridge, uniq, channel="web", identity=identity)
    web, page = await WebChatClient.open_page(_base_url(bridge), identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text

    prompt = uniq("l27-web-prompt")
    image_url = f"https://cdn.example.com/{uniq('l27-web-img')}.jpg"
    # The agent-initiated form notify names the visitor pair as the composite recipient,
    # exactly as the media/options notifies do.
    await bridge.api().post(
        "/api/notifications",
        json={
            "message": prompt,
            "channel": "web",
            "recipient": web.recipient,
            "schema": _WEB_SCHEMA,
            "media": [{"kind": "image", "url": image_url, "caption": "figure"}],
        },
    )

    def _is_form_card(event: str, data: dict) -> bool:
        return event == "chat.form" and data.get("text") == prompt

    frames = await web.frames(until=_is_form_card)
    card = next(data for event, data in frames if _is_form_card(event, data))
    # The card carries the prompt, the schema verbatim, the media, and the server-minted
    # submission token — and it is the ONE form frame on the stream (a replay read is the
    # settled census: the notify appended exactly one card).
    assert card["schema"] == _WEB_SCHEMA
    assert card["media"] == [{"kind": "image", "url": image_url, "caption": "figure"}]
    token = card["token"]
    assert isinstance(token, str)
    assert token
    replay = await web.frames()
    assert [data["text"] for event, data in replay if event == "chat.form"] == [prompt]

    # A submission bridges as ONE guest turn: the tool payload's ``form`` is the values
    # verbatim and its ``message`` is the ``label: value`` text rendered from the STORED
    # schema's titles.
    first_marker = uniq("l27-web-a")
    submitted = await _post_form(bridge, token, {"topic": first_marker, "count": 4}, cookies=web.cookies)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["data"]["message_id"]
    first_turn = await _recorded_form_turn(bridge, first_marker)
    assert first_turn["form"] == {"topic": first_marker, "count": 4}
    assert first_turn["message"] == f"Topic: {first_marker}\nCount: 4"

    # RESUBMIT: the record is read, never claimed — the second submission is its own
    # second guest turn.
    second_marker = uniq("l27-web-b")
    resubmitted = await _post_form(bridge, token, {"topic": second_marker, "count": 5}, cookies=web.cookies)
    assert resubmitted.status_code == 200, resubmitted.text
    second_turn = await _recorded_form_turn(bridge, second_marker)
    assert second_turn["form"] == {"topic": second_marker, "count": 5}

    # A FOREIGN session's POST and a never-minted token answer the ONE uniform 404 —
    # same status, same body, no token/ownership oracle.
    foreign, foreign_page = await WebChatClient.open_page(
        _base_url(bridge), identity, store_url=bridge.stack.resources.redis_url
    )
    assert foreign_page.status_code == 200, foreign_page.text
    foreign_refusal = await _post_form(bridge, token, {"topic": "x", "count": 1}, cookies=foreign.cookies)
    unknown_refusal = await _post_form(bridge, uuid.uuid4().hex, {"topic": "x", "count": 1}, cookies=web.cookies)
    assert foreign_refusal.status_code == 404, foreign_refusal.text
    assert unknown_refusal.status_code == 404, unknown_refusal.text
    assert foreign_refusal.json() == unknown_refusal.json()

    # SCHEMA-VIOLATING values are transport, not validation failures: the door never
    # checks them against the stored schema, so they bridge verbatim as the turn's form.
    violating_marker = uniq("l27-web-c")
    violating = {"topic": violating_marker, "count": "not-a-number", "extra": {"nested": True}}
    accepted = await _post_form(bridge, token, violating, cookies=web.cookies)
    assert accepted.status_code == 200, accepted.text
    violating_turn = await _recorded_form_turn(bridge, violating_marker)
    assert violating_turn["form"] == violating


@_whatsapp_mock_leg
async def test_whatsapp_notify_form_sends_namespaced_flow_and_reply_bridges_coerced_form(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = _fresh_phone_id()
    await _form_record_route(bridge, uniq, channel="whatsapp", identity=identity)
    wa_id = _fresh_wa_id()
    prompt = uniq("l27-wa-prompt")

    await bridge.api().post(
        "/api/notifications",
        json={
            "message": prompt,
            "channel": "whatsapp",
            "recipient": wa_id,
            "schema": _whatsapp_schema("flow-and-coerce"),
        },
    )

    # The notify rendered the SAME Flow machinery a form ask uses (one created +
    # published Flow), but its token rides the ``tai42-nf:`` namespace and NO pending
    # reservation exists on the pair — the token routes the reply, not the pair.
    send = await wait_whatsapp_send(bridge.fake_whatsapp, prompt)
    assert send["type"] == "interactive"
    interactive = send["payload"]["interactive"]
    assert interactive["type"] == "flow"
    assert len(bridge.fake_whatsapp.flows) == 1
    assert bridge.fake_whatsapp.published_flows == [bridge.fake_whatsapp.flows[0]["id"]]
    flow_token = interactive["action"]["parameters"]["flow_token"]
    assert flow_token.startswith("tai42-nf:")
    assert _stack_redis_get(bridge, _whatsapp_pending_key(BRIDGE_WHATSAPP_PHONE_ID, wa_id)) is None

    # The signed completed-Flow reply carrying that token enters the conversation as a
    # structured guest turn: values coerced to the schema's types (Flow inputs arrive as
    # strings), the token dropped, the text rendered from the schema's titles.
    marker = uniq("l27-wa-a")
    reply = bridge.whatsapp_nfm_reply(
        phone_number_id=identity,
        wa_id=wa_id,
        response={"flow_token": flow_token, "topic": marker, "amount": "4"},
    )
    resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
    assert resp.status_code == 200, resp.text
    turn = await _recorded_form_turn(bridge, marker)
    assert turn["form"] == {"topic": marker, "amount": 4}
    assert turn["message"] == f"Topic: {marker}\nAmount: 4"


@_whatsapp_mock_leg
async def test_whatsapp_notify_form_reply_never_touches_a_pending_ask_on_the_same_pair(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    wa_id = _fresh_wa_id()
    question = uniq("l27-wa-ask-q")
    ask_answer = uniq("l27-wa-ask-a")
    # If the bridged notify-form turn routes to a target on the default identity, this
    # scripted reply resolves it (the l7 defensive-script pattern).
    script_reply(bridge.llm_stub, uniq("l27-wa-bridge"))

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool("ask_user", {"question": question, "channel": "whatsapp", "recipient": wa_id})
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        await wait_send_to(bridge.fake_whatsapp, to=wa_id, needle=question)

        # An ask-less form notify to the SAME pair while the ask is live.
        prompt = uniq("l27-wa-fence-prompt")
        await bridge.api().post(
            "/api/notifications",
            json={
                "message": prompt,
                "channel": "whatsapp",
                "recipient": wa_id,
                "schema": _whatsapp_schema("ask-fence"),
            },
        )
        form_send = await wait_send_to(bridge.fake_whatsapp, to=wa_id, needle=prompt)
        flow_token = form_send[0]["payload"]["interactive"]["action"]["parameters"]["flow_token"]
        assert flow_token.startswith("tai42-nf:")

        # Its completed-Flow reply lands on the pair — and is NOT an answer: the token
        # prefix routes it out before any pending peek, so the reservation survives
        # untouched and the ask stays live.
        masquerade = bridge.whatsapp_nfm_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            response={"flow_token": flow_token, "topic": uniq("l27-wa-fence"), "amount": "1"},
        )
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, masquerade, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        assert not ask_task.done()
        assert _stack_redis_get(bridge, _whatsapp_pending_key(BRIDGE_WHATSAPP_PHONE_ID, wa_id)) is not None

        # The still-pending ask is answered by the genuine correlated reply.
        genuine = bridge.whatsapp_inbound(phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=wa_id, text=ask_answer)
        genuine_resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, genuine, port=bridge.stack.port_b)
        assert genuine_resp.status_code == 200, genuine_resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == ask_answer


@_whatsapp_mock_leg
async def test_whatsapp_notify_form_schema_sidecar_miss_degrades_reply_to_raw_values(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = _fresh_phone_id()
    await _form_record_route(bridge, uniq, channel="whatsapp", identity=identity)
    wa_id = _fresh_wa_id()
    prompt = uniq("l27-wa-miss-prompt")

    await bridge.api().post(
        "/api/notifications",
        json={"message": prompt, "channel": "whatsapp", "recipient": wa_id, "schema": _whatsapp_schema("sidecar-miss")},
    )
    send = await wait_whatsapp_send(bridge.fake_whatsapp, prompt)
    flow_token = send["payload"]["interactive"]["action"]["parameters"]["flow_token"]
    assert flow_token.startswith("tai42-nf:")
    schema_hash = flow_token.split(":", 2)[1]

    # Flush the durable schema sidecar the send wrote (the delete count proves the send
    # DID write it): the reply carries only the hash, so the entry cannot be repopulated.
    assert _stack_redis_delete(bridge, _whatsapp_schema_sidecar_key(schema_hash)) == 1

    # The reply still lands — DEGRADED, never dropped: raw string values (no coercion,
    # no schema to coerce by), labels falling back to the raw field keys.
    marker = uniq("l27-wa-miss")
    reply = bridge.whatsapp_nfm_reply(
        phone_number_id=identity,
        wa_id=wa_id,
        response={"flow_token": flow_token, "topic": marker, "amount": "7"},
    )
    resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
    assert resp.status_code == 200, resp.text
    turn = await _recorded_form_turn(bridge, marker)
    assert turn["form"] == {"topic": marker, "amount": "7"}
    assert turn["message"] == f"topic: {marker}\namount: 7"


async def test_form_notify_to_a_channel_without_the_capability_is_501_and_leaves_no_feed_entry(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("l27-cap")
    audience = uniq("l27-cap-aud")
    # Twilio advertises no ``supports_form_notifications``: the central guard refuses the
    # send as a 501 BEFORE any feed write — even though the call is audience-addressed.
    refused = await bridge.api().post(
        "/api/notifications",
        json={
            "message": marker,
            "channel": "twilio",
            "recipient": BRIDGE_TWILIO_CLIENT,
            "audience": audience,
            "schema": _WEB_SCHEMA,
        },
        expect=501,
    )
    assert "does not support form notifications" in refused["error"]

    # No phantom feed entry (the refusal fired before the audience record was written)
    # and nothing on the twilio wire. The notify door is synchronous, so the feed read
    # right after the 501 is the settled census.
    listing = await bridge.api().get("/api/notifications")
    assert all(record["message"] != marker for record in listing["notifications"])
    assert bridge.fake_twilio.sends_matching(marker) == []
