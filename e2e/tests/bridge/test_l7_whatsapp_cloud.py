"""L7 — whatsapp leg (the second provider).

Meta's GET verification handshake; a signed POST inbound routed to a turn whose answer sends
FROM the correct ``phone_number_id``; a bad signature is 401 with no turn; two
``phone_number_id``s under one credential fire two routes; and an ask_user round-trip over
whatsapp (deliver → correlated reply → answer returns) — the coverage every sibling
channel has.

The rich-messaging legs extend that spine: a select ask renders natively — interactive
reply buttons for a few short options, an interactive list for more, the numbered-text
fallback past those caps — and a tap answers by the question-bound id; a tap with no
pending question (or a stale id) is a non-answer that bridges the reply title without
disturbing a live ask; ``notify_user`` carries media (image parts) and a template send;
and the split recipient policy admits a freeform send to any recipient while fencing a
template to the allowlist or a known contact.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

from _bridge_support import (
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    cancel_and_join,
    post_inbound,
    request_mentions,
    script_reply,
    tool_text,
    wait_whatsapp_send,
    whatsapp_get_verify,
)

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
    BRIDGE_WHATSAPP_PHONE_ID_B,
    BRIDGE_WHATSAPP_PHONE_ID_C,
)


def _fresh_wa_id() -> str:
    """A distinct human wa_id per leg, so no two asks contend for the single
    pending-question slot on one ``(phone_number_id, wa_id)`` pair."""
    return f"1650{uuid.uuid4().int % 10**9:09d}"


def _fresh_phone_id() -> str:
    """A distinct sending ``phone_number_id`` per leg (a route's ``our_identity``)."""
    return f"9{uuid.uuid4().int % 10**14:014d}"


async def test_verify_handshake(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    challenge = uniq("challenge")
    good = bridge.fake_whatsapp.verify_params(verify_token=bridge.whatsapp_verify_token, challenge=challenge)
    resp = await whatsapp_get_verify(bridge.stack, good)
    assert resp.status_code == 200
    assert resp.text == challenge

    # A wrong verify token is refused — the challenge is never echoed.
    bad = bridge.fake_whatsapp.verify_params(verify_token="wrong-token", challenge=challenge)
    bad_resp = await whatsapp_get_verify(bridge.stack, bad)
    assert bad_resp.status_code == 403


async def test_signed_inbound_runs_turn_and_bad_signature_is_denied(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    exec_key = uniq("l7-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("l7-route").replace("_", "-")
    # Its own phone_number_id so it never collides with the two-ids leg on the shared stack.
    identity = BRIDGE_WHATSAPP_PHONE_ID_C
    await bridge.create_channel_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=exec_key,
        channel="whatsapp",
        our_identity=identity,
    )
    port = bridge.stack.port_b

    # A bad signature is 401 and starts no turn.
    bad = bridge.whatsapp_inbound(phone_number_id=identity, wa_id=BRIDGE_WHATSAPP_CLIENT, text="ignored", valid=False)
    bad_resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, bad, port=port)
    assert bad_resp.status_code == 401
    assert bridge.llm_stub.requests == []

    # A genuinely-signed inbound runs the turn; the answer sends FROM the routed phone_number_id.
    answer = uniq("l7-ans")
    script_reply(bridge.llm_stub, f"wa {answer}")
    good = bridge.whatsapp_inbound(phone_number_id=identity, wa_id=BRIDGE_WHATSAPP_CLIENT, text="hello whatsapp")
    good_resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, good, port=port)
    assert good_resp.status_code == 200
    send = await wait_whatsapp_send(bridge.fake_whatsapp, answer)
    assert send["phone_number_id"] == identity
    assert send["to"] == BRIDGE_WHATSAPP_CLIENT


async def test_two_phone_number_ids_two_agents(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    exec_a = uniq("l7-exec-a")
    exec_b = uniq("l7-exec-b")
    await bridge.mint_key(user_id=exec_a, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_b, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=uniq("l7-route-a").replace("_", "-"),
        agent="tools_agent",
        execution_key=exec_a,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
    )
    await bridge.create_channel_route(
        route_name=uniq("l7-route-b").replace("_", "-"),
        agent="tools_agent",
        execution_key=exec_b,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID_B,
    )
    port = bridge.stack.port_b

    answer_a = uniq("l7-two-a")
    answer_b = uniq("l7-two-b")
    script_reply(bridge.llm_stub, f"pid-a {answer_a}", f"pid-b {answer_b}")

    inbound_a = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text="to pid A"
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound_a, port=port)).status_code == 200
    send_a = await wait_whatsapp_send(bridge.fake_whatsapp, answer_a)
    assert send_a["phone_number_id"] == BRIDGE_WHATSAPP_PHONE_ID

    inbound_b = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID_B, wa_id=BRIDGE_WHATSAPP_CLIENT, text="to pid B"
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound_b, port=port)).status_code == 200
    send_b = await wait_whatsapp_send(bridge.fake_whatsapp, answer_b)
    assert send_b["phone_number_id"] == BRIDGE_WHATSAPP_PHONE_ID_B


async def test_ask_user_round_trip_over_whatsapp(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    question = uniq("l7-ask-q")
    ask_answer = uniq("l7-ask-a")

    async def ask() -> object:
        # ask_user over whatsapp delivers FROM the default phone_number_id to the
        # allowlisted wa_id and stores a Tier-2 pending correlation on that pair.
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask_user", {"question": question, "channel": "whatsapp", "recipient": BRIDGE_WHATSAPP_CLIENT}
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        await wait_whatsapp_send(bridge.fake_whatsapp, question)
        # The signed reply on the same pair correlates and forwards to the callback door.
        reply = bridge.whatsapp_inbound(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=ask_answer
        )
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == ask_answer


async def _ask_select(bridge: BridgeHarness, *, question: str, recipient: str, options: list[str]) -> asyncio.Task:
    """Open a select ask over whatsapp (delivered FROM the default phone_number_id) and
    return the still-running task; the caller drives the reply and awaits the answer."""

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask_user",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": recipient,
                    "answer_format": "select",
                    "options": options,
                },
            )
        return result.data

    return asyncio.create_task(ask())


async def test_select_renders_buttons_and_tap_answers_option_text(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-buttons-q")
    options = ["Refund", "Replace", "Cancel"]  # <= 3 short unique options -> reply buttons
    wa_id = _fresh_wa_id()

    ask_task = await _ask_select(bridge, question=question, recipient=wa_id, options=options)
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        # Rendered as an interactive reply-buttons message, one button per option.
        assert send["type"] == "interactive"
        interactive = send["payload"]["interactive"]
        assert interactive["type"] == "button"
        buttons = interactive["action"]["buttons"]
        assert [b["reply"]["title"] for b in buttons] == options
        # The chosen button carries the question-bound id {interaction_id}:{index}.
        chosen = buttons[1]["reply"]
        assert chosen["id"].endswith(":1")
        reply = bridge.whatsapp_interactive_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            reply_kind="button_reply",
            reply_id=chosen["id"],
            title=chosen["title"],
        )
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    # The tap answers with the exact option text bound to that button's index.
    assert resolved == options[1]


async def test_select_renders_list_and_pick_answers_option_text(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-list-q")
    # > 3 options (past the button cap) but <= 10 short rows -> interactive list.
    options = ["North", "South", "East", "West", "Central"]
    wa_id = _fresh_wa_id()

    ask_task = await _ask_select(bridge, question=question, recipient=wa_id, options=options)
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        assert send["type"] == "interactive"
        interactive = send["payload"]["interactive"]
        assert interactive["type"] == "list"
        rows = interactive["action"]["sections"][0]["rows"]
        assert [r["title"] for r in rows] == options
        chosen = rows[3]
        assert chosen["id"].endswith(":3")
        reply = bridge.whatsapp_interactive_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            reply_kind="list_reply",
            reply_id=chosen["id"],
            title=chosen["title"],
        )
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == options[3]


async def test_over_cap_select_falls_back_to_numbered_text_answered_by_typed_reply(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-fallback-q")
    # 11 options exceeds the list-row cap -> the numbered-text fallback (today's shape).
    options = [f"Choice {index}" for index in range(1, 12)]
    wa_id = _fresh_wa_id()

    ask_task = await _ask_select(bridge, question=question, recipient=wa_id, options=options)
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        assert send["type"] == "text"
        assert "Reply with the text of one option." in send["body"]
        # The human types an option verbatim; the correlated text reply answers the ask.
        reply = bridge.whatsapp_inbound(phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=wa_id, text=options[4])
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == options[4]


async def test_button_reply_without_pending_bridges_the_title(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    exec_key = uniq("l7-btn-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    identity = _fresh_phone_id()
    await bridge.create_channel_route(
        route_name=uniq("l7-btn-route").replace("_", "-"),
        agent="tools_agent",
        execution_key=exec_key,
        channel="whatsapp",
        our_identity=identity,
    )
    answer = uniq("l7-btn-ans")
    script_reply(bridge.llm_stub, f"echoed {answer}")

    # A button tap with NO pending question on this pair is not an answer: the tap's
    # human-readable TITLE routes to the bridge like any unrelated message (the wire id
    # never reaches the agent), and a well-signed tap is never a 5xx.
    title = uniq("l7-btn-title")
    reply = bridge.whatsapp_interactive_reply(
        phone_number_id=identity,
        wa_id=_fresh_wa_id(),
        reply_kind="button_reply",
        reply_id="0000-none:0",
        title=title,
    )
    resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
    assert resp.status_code == 200, resp.text

    send = await wait_whatsapp_send(bridge.fake_whatsapp, answer)
    assert send["phone_number_id"] == identity
    # The bridged turn saw the reply TITLE, not the wire id.
    assert any(request_mentions(bridge.llm_stub, index, title) for index in range(len(bridge.llm_stub.requests)))


async def test_stale_button_tap_restores_pending_ask(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    question = uniq("l7-stale-q")
    options = ["Yes", "No"]  # 2 short options -> reply buttons
    wa_id = _fresh_wa_id()
    # If the stale title routes to a turn on the default identity, this reply resolves it.
    script_reply(bridge.llm_stub, uniq("l7-stale-bridge"))

    ask_task = await _ask_select(bridge, question=question, recipient=wa_id, options=options)
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        correct = send["payload"]["interactive"]["action"]["buttons"][1]["reply"]  # bound to "No"
        # A STALE tap (an id whose interaction part is not this ask's) is NOT an answer:
        # the pending question is restored untouched and the tap's title is bridged.
        stale = bridge.whatsapp_interactive_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            reply_kind="button_reply",
            reply_id="0000-gone:0",
            title=uniq("l7-stale-title"),
        )
        stale_resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, stale, port=bridge.stack.port_b)
        assert stale_resp.status_code == 200, stale_resp.text  # non-answer, never a 5xx
        assert not ask_task.done()  # the live ask survived the stale tap

        # The correct bound tap now resolves the SAME still-pending ask.
        good = bridge.whatsapp_interactive_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            reply_kind="button_reply",
            reply_id=correct["id"],
            title=correct["title"],
        )
        good_resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, good, port=bridge.stack.port_b)
        assert good_resp.status_code == 200, good_resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == options[1]


async def test_notify_media_sends_body_then_one_image_per_item(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("l7-media")
    wa_id = _fresh_wa_id()  # freeform media reaches any recipient (no allowlist fence)
    image_a = f"https://cdn.example.com/{marker}-a.jpg"
    image_b = f"https://cdn.example.com/{marker}-b.jpg"

    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        result = await mcp.call_tool(
            "notify_user",
            {
                "message": marker,
                "channel": "whatsapp",
                "recipient": wa_id,
                "media": [
                    {"kind": "image", "url": image_a, "caption": f"{marker} first"},
                    {"kind": "image", "url": image_b, "caption": f"{marker} second"},
                ],
            },
        )
    assert "notification sent via 'whatsapp'" in tool_text(result)

    sends = [record for record in bridge.fake_whatsapp.sent if record["to"] == wa_id]
    # The freeform body first, then one image message per item, each with its own wamid.
    assert [record["type"] for record in sends] == ["text", "image", "image"]
    assert sends[0]["body"] == marker
    assert sends[1]["payload"]["image"] == {"link": image_a, "caption": f"{marker} first"}
    assert sends[2]["payload"]["image"] == {"link": image_b, "caption": f"{marker} second"}
    assert all(record["wamid"] for record in sends)


async def test_notify_template_sends_template_payload_to_allowlisted_recipient(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    name = uniq("l7_tmpl")
    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        result = await mcp.call_tool(
            "notify_user",
            {
                "message": f"{name} human-readable equivalent",
                "channel": "whatsapp",
                # env-allowlisted -> the template recipient fence admits it.
                "recipient": BRIDGE_WHATSAPP_CLIENT,
                "template": {"name": name, "language": "en_US", "parameters": ["Ada", "3pm"]},
            },
        )
    assert "notification sent via 'whatsapp'" in tool_text(result)

    sends = bridge.fake_whatsapp.payloads_matching(name)
    assert len(sends) == 1
    assert sends[0]["type"] == "template"
    assert sends[0]["to"] == BRIDGE_WHATSAPP_CLIENT
    template = sends[0]["payload"]["template"]
    assert template["name"] == name
    assert template["language"] == {"code": "en_US"}
    assert template["components"] == [
        {"type": "body", "parameters": [{"type": "text", "text": "Ada"}, {"type": "text", "text": "3pm"}]}
    ]


async def test_notify_rich_content_capability_and_channel_guards(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("l7-guard")
    media = [{"kind": "image", "url": f"https://cdn.example.com/{marker}.jpg"}]
    template = {"name": f"{marker}_t", "language": "en_US"}

    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        # A channel WITHOUT the capability (twilio advertises neither) -> 501, never a
        # silent downgrade to a freeform send.
        media_501 = await mcp.call_tool(
            "notify_user",
            {"message": marker, "channel": "twilio", "recipient": BRIDGE_TWILIO_CLIENT, "media": media},
            raise_on_error=False,
        )
        assert media_501.is_error
        assert "does not support media notifications" in tool_text(media_501)

        template_501 = await mcp.call_tool(
            "notify_user",
            {"message": marker, "channel": "twilio", "recipient": BRIDGE_TWILIO_CLIENT, "template": template},
            raise_on_error=False,
        )
        assert template_501.is_error
        assert "does not support template notifications" in tool_text(template_501)

        # Rich content with NO named channel -> 400: the internal sink stores no rich content.
        media_400 = await mcp.call_tool("notify_user", {"message": marker, "media": media}, raise_on_error=False)
        assert media_400.is_error
        assert "require a named channel" in tool_text(media_400)

        template_400 = await mcp.call_tool(
            "notify_user", {"message": marker, "template": template}, raise_on_error=False
        )
        assert template_400.is_error
        assert "require a named channel" in tool_text(template_400)

    # A refused rich send left nothing dispatched: the capability/channel guard fired
    # before twilio was ever asked to send.
    assert bridge.fake_twilio.sends_matching(marker) == []


async def test_freeform_notify_reaches_an_unlisted_recipient(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    # WhatsApp diverges from telegram/slack/twilio by design: a freeform send is unfenced
    # (Meta's own 24-hour window is the fence), so an unlisted recipient succeeds.
    marker = uniq("l7-freeform")
    wa_id = _fresh_wa_id()
    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        result = await mcp.call_tool("notify_user", {"message": marker, "channel": "whatsapp", "recipient": wa_id})
    assert "notification sent via 'whatsapp'" in tool_text(result)

    sends = bridge.fake_whatsapp.sends_matching(marker)
    assert len(sends) == 1
    assert sends[0]["to"] == wa_id
    assert sends[0]["type"] == "text"


async def test_template_to_cold_unlisted_recipient_is_refused(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("l7-cold")
    wa_id = _fresh_wa_id()  # never allowlisted, never seen inbound
    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        result = await mcp.call_tool(
            "notify_user",
            {
                "message": f"{marker} body",
                "channel": "whatsapp",
                "recipient": wa_id,
                "template": {"name": marker, "language": "en_US"},
            },
            raise_on_error=False,
        )
    assert result.is_error
    assert "refused" in tool_text(result)
    assert bridge.fake_whatsapp.payloads_matching(marker) == []


async def test_template_to_known_contact_recipient_succeeds(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    marker = uniq("l7-known")
    wa_id = _fresh_wa_id()  # unlisted, but about to open Meta's window
    # A reply in case the priming inbound routes to a turn on the default identity.
    script_reply(bridge.llm_stub, uniq("l7-known-bridge"))

    # Prime: a signed inbound from wa_id to the default send-from identity records the
    # (phone_number_id, wa_id) known-contact marker BEFORE the door acks — the marker is
    # written for every authenticated inbound, ahead of any routing.
    priming = bridge.whatsapp_inbound(phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=wa_id, text=uniq("l7-known-in"))
    prime_resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, priming, port=bridge.stack.port_b)
    assert prime_resp.status_code == 200, prime_resp.text

    # A template to that now-known (still unlisted) contact is admitted — the known-contact
    # lookup keys on the resolved send-from id, which equals the priming inbound's identity.
    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        result = await mcp.call_tool(
            "notify_user",
            {
                "message": f"{marker} body",
                "channel": "whatsapp",
                "recipient": wa_id,
                "template": {"name": marker, "language": "en_US"},
            },
        )
    assert "notification sent via 'whatsapp'" in tool_text(result)

    sends = bridge.fake_whatsapp.payloads_matching(marker)
    assert len(sends) == 1
    assert sends[0]["type"] == "template"
    assert sends[0]["to"] == wa_id
