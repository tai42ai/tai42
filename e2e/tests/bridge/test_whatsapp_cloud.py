"""Whatsapp leg (the second provider).

Meta's GET verification handshake; a signed POST inbound routed to a turn whose answer sends
FROM the correct ``phone_number_id``; a bad signature is 401 with no turn; two
``phone_number_id``s under one credential fire two routes; and an ask round-trip over
whatsapp (deliver → correlated reply → answer returns) — the coverage every sibling
channel has.

The rich-messaging legs extend that spine: a select ask renders natively — interactive
reply buttons for a few short options, an interactive list for more, the numbered-text
fallback past those caps — and a tap answers by the question-bound id; a tap with no
pending question (or a stale id) is a non-answer that bridges the reply title without
disturbing a live ask; ``notify_user`` carries media (image parts) and a template send;
and the split recipient policy admits a freeform send to any recipient while fencing a
template to the allowlist or a known contact.

The Flow encryption provisioning door registers the public half of the stack's business key
for the default or a named sending number, and refuses a caller with no key (401) or a
non-admin key (403) before any Graph call.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import Callable

import pytest

from tai42_e2e.httpapi import ApiClient
from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
    BRIDGE_WHATSAPP_PHONE_ID_B,
    BRIDGE_WHATSAPP_PHONE_ID_C,
)
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    cancel_and_join,
    post_inbound,
    request_mentions,
    script_reply,
    tool_text,
    wait_channel_send_count,
    wait_probe_record,
    wait_whatsapp_send,
    whatsapp_get_verify,
)

# The scripted-LLM + FakeWhatsApp round-trips (verify handshake, correlated reply, ask/select)
# are the mock leg for BOTH the 'whatsapp' channel seam and the 'llm' seam (build_bridge_stack
# wires the LLM env too, so TAI_E2E_REAL=llm also sends the bridge LLM to the live provider).
# Either selection real breaks the stub scripting, so the module steps aside; the real legs run
# on the dedicated e2e creds host, not in CI. Inert in the default mock run — both
# is_real checks are False, so collection is byte-for-byte today's.
pytestmark = [
    pytest.mark.needs("kind:identity"),
    pytest.mark.skipif(
        HarnessSettings().is_real("whatsapp") or HarnessSettings().is_real("llm"),
        reason="FakeWhatsApp + scripted-LLM is the 'whatsapp'/'llm' mock leg; real legs on the creds host",
    ),
]


# The smallest form answer schema: a text field, an integer field, a date field, and an
# ``inputMode: email`` string field, all required — the integer is what the Flow returns as a
# string and the coercion turns back into an int; the ``format: date`` field renders Meta's date
# picker and returns a ``YYYY-MM-DD`` string the decode leaves unchanged; ``contact`` declares the
# email keyboard, so its Flow ``TextInput`` carries Meta's ``input-type: email`` while the value
# still arrives as the typed string.
_FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string"},
        "amount": {"type": "integer"},
        "when": {"type": "string", "format": "date"},
        "contact": {"type": "string", "inputMode": "email"},
    },
    "required": ["label", "amount", "when", "contact"],
}


# A schema carrying the four new form controls, all on one page so the range pair merges:
# ``colour`` a chips string enum (2..20 choices) → a ChipsSelector; ``size`` a chips string enum
# of ONE choice (below Meta's 2-choice floor) → the documented degrade to the default control (a
# RadioButtonsGroup); ``notes`` a ``textarea`` string with a drawn ``maxLength`` → a TextArea
# carrying ``max-length``; ``start``/``end`` a ``rangeStart`` date pair with a span → ONE
# range-mode CalendarPicker. The page carries a caller ``button_label`` (the footer's text).
_CONTROLS_SCHEMA = {
    "type": "object",
    "properties": {
        "colour": {"type": "string", "enum": ["red", "green", "blue"], "control": "chips"},
        "size": {"type": "string", "enum": ["one"], "control": "chips"},
        "notes": {"type": "string", "control": "textarea", "maxLength": 600},
        "start": {"type": "string", "format": "date"},
        "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 2, "maxDays": 5},
    },
}
_CONTROLS_PAGES = [
    {"title": "Details", "fields": ["colour", "size", "notes", "start", "end"], "button_label": "Send it"}
]

# A ``rangeStart`` date pair the WhatsApp renderer must NOT merge into one range control, because
# a SIBLING field's ``visibleWhen`` names the pair's END — merging would leave that sibling's
# ``${form.end}`` reference reading the one range control's object value. The pair draws as two
# separate date controls instead (the documented degrade): ``start`` (no constraints) a bare
# DatePicker, ``end`` (its ``rangeStart`` is a constraint) a single-mode CalendarPicker.
_RANGE_WITH_DEPENDENT_SIBLING_SCHEMA = {
    "type": "object",
    "properties": {
        "start": {"type": "string", "format": "date"},
        "end": {"type": "string", "format": "date", "rangeStart": "start", "minDays": 1, "maxDays": 30},
        "detail": {"type": "string", "visibleWhen": {"field": "end", "equals": "2026-02-04"}},
    },
}


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFERENCE_RE = re.compile(r"\$\{(?:data|form|screen)\.([^}]+)\}")


def _flow_control_names(flow_json: dict) -> list[str]:
    """Every field component's ``name`` across the Flow's screens (the identifier-safe
    wire names — the completed form is echoed back keyed by the fields' labels instead)."""
    return [
        child["name"]
        for screen in flow_json["screens"]
        for child in screen["layout"]["children"]
        if child.get("type") in {"TextInput", "Dropdown", "OptIn", "DatePicker"}
    ]


def _text_input_by_name(flow_json: dict, name: str) -> dict:
    """The single ``TextInput`` component carrying ``name`` across the Flow's screens."""
    matches = [
        child
        for screen in flow_json["screens"]
        for child in screen["layout"]["children"]
        if child.get("type") == "TextInput" and child.get("name") == name
    ]
    assert len(matches) == 1, f"expected exactly one TextInput named {name!r}, saw {len(matches)}"
    return matches[0]


def _layout_children(flow_json: dict) -> list[dict]:
    """Every layout child across the Flow's screens, flattened (an ``If`` wrapper's ``then`` unwrapped).

    A ``visibleWhen`` field renders under one or more nested ``If`` components; this descends into
    their ``then`` lists so a control is found whether or not it is conditionally gated."""
    out: list[dict] = []

    def _walk(children: list[dict]) -> None:
        for child in children:
            if child.get("type") == "If":
                _walk(child.get("then", []))
            else:
                out.append(child)

    for screen in flow_json["screens"]:
        _walk(screen["layout"]["children"])
    return out


def _components_of_type(flow_json: dict, ctype: str) -> list[dict]:
    """Every layout component of type ``ctype`` across the Flow's screens."""
    return [child for child in _layout_children(flow_json) if child.get("type") == ctype]


def _component_by_name(flow_json: dict, ctype: str, name: str) -> dict:
    """The single ``ctype`` component carrying ``name`` across the Flow's screens."""
    matches = [c for c in _components_of_type(flow_json, ctype) if c.get("name") == name]
    assert len(matches) == 1, f"expected exactly one {ctype} named {name!r}, saw {len(matches)}"
    return matches[0]


def _terminal_footer_label(flow_json: dict) -> str:
    """The ``label`` of the terminal screen's Footer (the form's submit button)."""
    footers = _components_of_type(flow_json, "Footer")
    assert footers, "the published Flow carries no Footer"
    return footers[-1]["label"]


def _assert_flow_wire_names_identifier_safe(flow_json: dict) -> None:
    """Every component ``name``, screen-``data`` key, navigate-``payload`` key and
    ``${data.…}`` / ``${form.…}`` reference the created Flow carries is in Meta's
    identifier grammar. Field labels and the terminal ``complete`` action's payload keys
    (the human-readable completion labels) are not wire names and may carry any character."""
    for reference in _REFERENCE_RE.findall(json.dumps(flow_json)):
        assert _IDENTIFIER_RE.fullmatch(reference), reference
    for name in _flow_control_names(flow_json):
        assert _IDENTIFIER_RE.fullmatch(name), name
    for screen in flow_json["screens"]:
        for data_key in screen.get("data", {}):
            assert _IDENTIFIER_RE.fullmatch(data_key), data_key
        for child in screen["layout"]["children"]:
            action = child.get("on-click-action")
            if isinstance(action, dict) and action.get("name") == "navigate":
                for payload_key in action.get("payload", {}):
                    assert _IDENTIFIER_RE.fullmatch(payload_key), payload_key


def _fresh_wa_id() -> str:
    """A distinct human wa_id per leg, so no two asks contend for the single
    pending-question slot on one ``(phone_number_id, wa_id)`` pair."""
    return f"1650{uuid.uuid4().int % 10**9:09d}"


def _fresh_phone_id() -> str:
    """A distinct sending ``phone_number_id`` per leg (a route's ``our_identity``)."""
    return f"9{uuid.uuid4().int % 10**14:014d}"


@pytest.mark.needs("kind:channels:whatsapp", "setting:seeded-access-control")
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:llm",
    "helper:whatsapp",
    "setting:agent",
    "setting:conversations",
    "setting:seeded-access-control",
)
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:llm",
    "helper:whatsapp",
    "setting:agent",
    "setting:conversations",
    "setting:seeded-access-control",
)
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_ask_round_trip_over_whatsapp(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    question = uniq("l7-ask-q")
    ask_answer = uniq("l7-ask-a")

    async def ask() -> object:
        # ask over whatsapp delivers FROM the default phone_number_id to the
        # allowlisted wa_id and stores a Tier-2 pending correlation on that pair.
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask", {"question": question, "channel": "whatsapp", "recipient": BRIDGE_WHATSAPP_CLIENT}
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
                "ask",
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_select_renders_buttons_and_tap_answers_option_text(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-buttons-q")
    options = ["Retry", "Replace", "Cancel"]  # <= 3 short unique options -> reply buttons
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:llm",
    "helper:whatsapp",
    "setting:agent",
    "setting:conversations",
    "setting:seeded-access-control",
)
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "probe-tools",
    "helper:whatsapp",
    "setting:conversations",
    "setting:seeded-access-control",
    "store:redis",
)
async def test_button_tap_carries_reply_id_onto_the_tool_payload_params(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Composed-path proof for the inbound-fidelity lane: a button tap with no pending
    question bridges as a fresh turn whose ROUTED TOOL PAYLOAD carries the tapped wire id
    under ``.params.reply_id`` — the exact seam a channel-agnostic tool consumer reads.

    Driven end to end over the live stack: signed whatsapp webhook → inbound decode →
    ``conversations.accept(params=…)`` → route → tool dispatch → the tool records what it
    received. Born red before the channel carried the tap's id into ``params`` (the payload
    had no ``params`` key, so ``.params.reply_id`` resolved to the ``"none"`` fallback)."""
    exec_key = uniq("l7-rid-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    identity = _fresh_phone_id()
    probe = uniq("l7-rid")
    # The tool records the id it actually received under .params; the fallback makes the
    # unfixed behavior (no params) an observably-different value, not a KeyError.
    await bridge.create_tool_channel_route(
        route_name=uniq("l7-rid-route").replace("_", "-"),
        tool="e2e_record",
        execution_key=exec_key,
        channel="whatsapp",
        our_identity=identity,
        start_expr=f'{{key: "{probe}", value: (.params.reply_id // "none")}}',
        reply_expr="null",
    )

    reply_id = "int-shipping:2"
    reply = bridge.whatsapp_interactive_reply(
        phone_number_id=identity,
        wa_id=_fresh_wa_id(),
        reply_kind="button_reply",
        reply_id=reply_id,
        title=uniq("l7-rid-title"),
    )
    resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
    assert resp.status_code == 200, resp.text

    recorded = await wait_probe_record(bridge, probe)
    # The tool saw the tapped wire id under .params.reply_id — not the "none" fallback.
    assert [entry["value"] for entry in recorded] == [reply_id]


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:llm",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_form_over_whatsapp_flow_and_nfm_reply_answers_with_a_typed_dict(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-form-q")
    good_answer = {
        "label": uniq("l7-form-label"),
        "amount": 4,
        "when": "2026-09-27",
        "contact": "alice@example.com",
    }
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "form",
                    "schema": _FORM_SCHEMA,
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        # The form ask is an interactive Flow send; its schema was created + published once
        # (uncached), and the send opens exactly that published flow.
        assert send["type"] == "interactive"
        interactive = send["payload"]["interactive"]
        assert interactive["type"] == "flow"
        assert len(bridge.fake_whatsapp.flows) == 1
        created = bridge.fake_whatsapp.flows[0]["id"]
        assert bridge.fake_whatsapp.published_flows == [created]
        # The ``contact`` field declared inputMode email, so its published Flow TextInput carries
        # Meta's input-type email (the email keyboard + Meta's own check); the value still returns
        # as the typed string, coerced only by the schema type (the integer below).
        flow_json = json.loads(bridge.fake_whatsapp.flows[0]["flow_json"])
        assert _text_input_by_name(flow_json, "contact")["input-type"] == "email"
        params = interactive["action"]["parameters"]
        assert params["flow_id"] == created
        flow_token = params["flow_token"]

        # The completed form returns as an nfm_reply matched by the flow token; a Flow number
        # input arrives as a string, so the answer carries ``"4"``. The date picker returns a
        # ``YYYY-MM-DD`` string the decode leaves unchanged.
        reply = bridge.whatsapp_nfm_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            response={
                "flow_token": flow_token,
                "label": good_answer["label"],
                "amount": "4",
                "when": "2026-09-27",
                "contact": good_answer["contact"],
            },
        )
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    # The Flow response is coerced to the schema's types (``"4"`` -> ``4``) and the
    # ``flow_token`` dropped — the caller gets the validated dict.
    assert resolved == good_answer


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_form_with_odd_property_names_maps_to_identifier_safe_flow_and_answers_under_original_key(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    # A schema property named with characters Meta's ${data.your_value} grammar forbids
    # still yields a publishable Flow: every component name/key/reference is identifier-safe,
    # and the completed form (which Meta relays keyed by the fields' human-readable LABELS)
    # answers under the ORIGINAL schema key.
    question = uniq("l7-odd-q")
    odd_key = "a.b=/c/4:d"
    label_value = uniq("l7-odd-label")
    schema = {
        "type": "object",
        "properties": {
            odd_key: {"type": "string", "title": "Label"},
            "amount": {"type": "integer", "title": "Amount"},
        },
        "required": [odd_key, "amount"],
    }
    good_answer = {odd_key: label_value, "amount": 5}
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "form",
                    "schema": schema,
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        assert send["type"] == "interactive"
        assert send["payload"]["interactive"]["type"] == "flow"
        # The created Flow JSON carries ONLY identifier-safe wire names — the odd key never
        # leaks into a name/key/reference (it survives only as the field's label).
        assert len(bridge.fake_whatsapp.flows) == 1
        flow_json = json.loads(bridge.fake_whatsapp.flows[0]["flow_json"])
        _assert_flow_wire_names_identifier_safe(flow_json)
        assert odd_key not in json.dumps(_flow_control_names(flow_json))

        flow_token = send["payload"]["interactive"]["action"]["parameters"]["flow_token"]
        # Meta relays the completed form keyed by the fields' human-readable labels ("Label"
        # for the odd property, "Amount"); a number input arrives as a string.
        reply = bridge.whatsapp_nfm_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            response={"flow_token": flow_token, "Label": label_value, "Amount": "5"},
        )
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    # The label-keyed reply mapped back to the schema keys and coerced ("5" -> 5).
    assert resolved == good_answer


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_form_controls_render_native_flow_components_and_answer_resolves(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    # A form ask carrying the four new controls, a per-page button label and a caller open-button
    # label publishes a Flow whose JSON draws each control natively, and a completed nfm_reply
    # resolves the ask with the chips enum string and the two range date strings.
    question = uniq("l7-ctrl-q")
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "form",
                    "schema": _CONTROLS_SCHEMA,
                    "pages": _CONTROLS_PAGES,
                    "wording": {"form": {"open": "Open the form"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        interactive = send["payload"]["interactive"]
        assert interactive["type"] == "flow"
        # The caller's open-button label rides the chat message as the Flow CTA (not Flow JSON).
        assert interactive["action"]["parameters"]["flow_cta"] == "Open the form"

        flow_json = json.loads(bridge.fake_whatsapp.flows[0]["flow_json"])
        # The chips string enum (3 choices, inside Meta's 2..20) draws a single-pick ChipsSelector.
        chips = _component_by_name(flow_json, "ChipsSelector", "colour")
        assert chips["max-selected-items"] == 1
        # The one-choice chips field falls below Meta's floor and degrades to the default control —
        # a RadioButtonsGroup, never a ChipsSelector.
        assert [c["name"] for c in _components_of_type(flow_json, "ChipsSelector")] == ["colour"]
        assert _component_by_name(flow_json, "RadioButtonsGroup", "size")
        # The textarea string draws a TextArea carrying the declared maximum.
        assert _component_by_name(flow_json, "TextArea", "notes")["max-length"] == 600
        # The rangeStart date pair draws ONE range-mode CalendarPicker, keyed by the start, its two
        # ends labelled by their own titles (here their names) and the span carried as min/max-days
        # (Meta counts the difference, one less than the platform's inclusive day count).
        ranges = [c for c in _components_of_type(flow_json, "CalendarPicker") if c.get("mode") == "range"]
        assert len(ranges) == 1
        assert ranges[0]["name"] == "start"
        assert ranges[0]["label"] == {"start-date": "start", "end-date": "end"}
        assert ranges[0]["min-days"] == 1
        assert ranges[0]["max-days"] == 4
        # The page's button_label is the terminal screen's footer (the submit button).
        assert _terminal_footer_label(flow_json) == "Send it"

        flow_token = interactive["action"]["parameters"]["flow_token"]
        # The single-pick ChipsSelector returns a one-element list; the merged range returns one
        # {start-date, end-date} object under the start field's label.
        reply = bridge.whatsapp_nfm_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            response={
                "flow_token": flow_token,
                "colour": ["green"],
                "start": {"start-date": "2026-02-01", "end-date": "2026-02-04"},
            },
        )
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    # The chips list unwrapped to the enum string, and the range object split onto the two date keys.
    assert resolved == {"colour": "green", "start": "2026-02-01", "end": "2026-02-04"}


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_form_range_pair_degrades_to_two_controls_when_a_sibling_depends_on_an_end(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-rangedep-q")
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "form",
                    "schema": _RANGE_WITH_DEPENDENT_SIBLING_SCHEMA,
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        await wait_whatsapp_send(bridge.fake_whatsapp, question)
        flow_json = json.loads(bridge.fake_whatsapp.flows[0]["flow_json"])
        # A sibling's visibleWhen names the end, so the pair is NOT merged: no range-mode control,
        # and the two date fields draw as their own controls (the start a bare DatePicker, the end
        # — its rangeStart a constraint — a single-mode CalendarPicker), so ${form.end} resolves.
        assert [c for c in _components_of_type(flow_json, "CalendarPicker") if c.get("mode") == "range"] == []
        assert _component_by_name(flow_json, "DatePicker", "start")
        assert _component_by_name(flow_json, "CalendarPicker", "end")["mode"] == "single"
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_form_wording_title_sets_the_single_screen_title(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-title-q")
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "form",
                    "schema": {"type": "object", "properties": {"label": {"type": "string"}}},
                    "wording": {"form": {"title": "Form details"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        await wait_whatsapp_send(bridge.fake_whatsapp, question)
        flow_json = json.loads(bridge.fake_whatsapp.flows[0]["flow_json"])
        # A page-less form's single screen takes the caller's wording.form.title as its top-bar title.
        assert flow_json["screens"][0]["title"] == "Form details"
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_form_open_label_is_reused_on_the_door_rejection_resend(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-resend-q")
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "form",
                    "schema": {
                        "type": "object",
                        "properties": {"amount": {"type": "integer"}},
                        "required": ["amount"],
                    },
                    "wording": {"form": {"open": "Start now"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        first = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        assert first["payload"]["interactive"]["action"]["parameters"]["flow_cta"] == "Start now"
        flow_token = first["payload"]["interactive"]["action"]["parameters"]["flow_token"]

        # A mismatched answer (a non-integer) is door-rejected and the channel recovers by
        # RE-SENDING a fresh Flow for the same ask — carrying the SAME open-button label.
        reply = bridge.whatsapp_nfm_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            response={"flow_token": flow_token, "amount": "not-a-number"},
        )
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        sends = await wait_channel_send_count(bridge.fake_whatsapp, question, 2)
        resend = sends[-1]
        assert resend["payload"]["interactive"]["type"] == "flow"
        assert resend["payload"]["interactive"]["action"]["parameters"]["flow_cta"] == "Start now"
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_select_list_button_reads_the_caller_choice_wording_label(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-choicelbl-q")
    # > 3 options render as an interactive list, whose opening button reads the caller's label.
    options = ["North", "South", "East", "West", "Central"]
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "select",
                    "options": options,
                    "wording": {"choice": {"label": "Pick a region"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        interactive = send["payload"]["interactive"]
        assert interactive["type"] == "list"
        # The list-opening button carries the caller's wording.choice.label, not the default "Choose an option".
        assert interactive["action"]["button"] == "Pick a region"
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_page_less_form_footer_reads_the_caller_submit_label(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-submit-q")
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "form",
                    "schema": {"type": "object", "properties": {"label": {"type": "string"}}},
                    "wording": {"form": {"submit": "Send answers"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        await wait_whatsapp_send(bridge.fake_whatsapp, question)
        flow_json = json.loads(bridge.fake_whatsapp.flows[0]["flow_json"])
        # A page-less form's single screen draws the caller's wording.form.submit as the terminal
        # footer (the submit button), not the generic "Submit".
        assert _terminal_footer_label(flow_json) == "Send answers"
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_numbered_text_select_reads_the_caller_reply_hint(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-hint-q")
    # 11 options exceed the list-row cap -> the numbered-text fallback, whose type-an-option line
    # the caller's wording.choice.reply_hint replaces (the default is covered by the over-cap test).
    options = [f"Choice {index}" for index in range(1, 12)]
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "select",
                    "options": options,
                    "wording": {"choice": {"reply_hint": "Type an option exactly."}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        assert send["type"] == "text"
        assert "Type an option exactly." in send["body"]
        assert "Reply with the text of one option." not in send["body"]
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_confirm_tier1_link_line_reads_the_caller_template(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-link-q")
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "confirm",
                    "wording": {"lines": {"answer_link": "Tap to reply: {url}"}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        send = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        assert send["type"] == "text"
        body = send["body"]
        # A Tier-1 confirm ask draws the caller's wording.lines.answer_link with its {url} token
        # filled by the callback link; the default "Answer here:" line is replaced.
        assert "Tap to reply: " in body
        assert "/api/interactions/callback/" in body
        assert "{url}" not in body
        assert "Answer here:" not in body
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:whatsapp",
    "setting:seeded-access-control",
    "setting:tool:ask",
    "topology:replicas",
)
async def test_door_rejection_resend_carries_the_caller_rejected_sentence(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("l7-reject-q")
    wa_id = _fresh_wa_id()

    async def ask() -> object:
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask",
                {
                    "question": question,
                    "channel": "whatsapp",
                    "recipient": wa_id,
                    "answer_format": "form",
                    "schema": {
                        "type": "object",
                        "properties": {"amount": {"type": "integer"}},
                        "required": ["amount"],
                    },
                    "wording": {"answer": {"rejected": "Not accepted — {reason} — try again."}},
                },
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        first = await wait_whatsapp_send(bridge.fake_whatsapp, question)
        flow_token = first["payload"]["interactive"]["action"]["parameters"]["flow_token"]

        # A mismatched answer (a non-integer) is door-rejected; the channel recovers by re-sending a
        # fresh Flow whose body carries the caller's refusal sentence off the pending record — proof
        # the record round-tripped on_mismatch (the RETRY re-send) and wording. The {reason} token is
        # filled with the door's own reason, and the generic lead is replaced.
        reply = bridge.whatsapp_nfm_reply(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID,
            wa_id=wa_id,
            response={"flow_token": flow_token, "amount": "not-a-number"},
        )
        resp = await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 200, resp.text
        sends = await wait_channel_send_count(bridge.fake_whatsapp, question, 2)
        body = sends[-1]["payload"]["interactive"]["body"]["text"]
        assert "Not accepted — " in body
        assert " — try again." in body
        assert "{reason}" not in body
        assert "Your last answer could not be accepted:" not in body
    finally:
        await cancel_and_join(ask_task)


@pytest.mark.needs(
    "kind:channels:whatsapp", "helper:whatsapp", "setting:router:notifications", "setting:tool:notify_user"
)
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


@pytest.mark.needs(
    "kind:channels:whatsapp", "helper:whatsapp", "setting:router:notifications", "setting:tool:notify_user"
)
async def test_notify_interactive_options_render_native_with_header_and_footer(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Composed-path proof for the rich notify vocabulary end to end over the live stack:
    ``notify_user`` carries typed reply + link options, a media header and a footer, and the
    whatsapp renderer emits ONE native interactive-buttons message whose wire payload carries
    the reply buttons, the link option as a body line, the image header object and the footer
    — contract model → skeleton ``notify_user`` operation → channel renderer → FakeWhatsApp
    wire, the same path an operator's rich notification travels."""
    marker = uniq("l7-rich")
    wa_id = _fresh_wa_id()  # freeform interactive reaches any recipient (no allowlist fence)
    header_url = f"https://cdn.example.com/{marker}-hdr.png"
    docs_url = f"https://docs.example.com/{marker}"

    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        result = await mcp.call_tool(
            "notify_user",
            {
                "message": marker,
                "channel": "whatsapp",
                "recipient": wa_id,
                "options": [
                    {"kind": "reply", "text": "Yes"},
                    {"kind": "reply", "text": "No"},
                    {"kind": "link", "label": "Docs", "url": docs_url},
                ],
                "header": {"kind": "image", "url": header_url},
                "footer": "powered by tai42",
            },
        )
    assert "notification sent via 'whatsapp'" in tool_text(result)

    sends = [record for record in bridge.fake_whatsapp.sent if record["to"] == wa_id]
    assert len(sends) == 1  # one native interactive frame, not a text downgrade
    interactive = sends[0]["payload"]["interactive"]
    # The reply options render as native reply buttons, one per reply option, in order.
    assert sends[0]["type"] == "interactive"
    assert interactive["type"] == "button"
    assert [button["reply"]["title"] for button in interactive["action"]["buttons"]] == ["Yes", "No"]
    # A reply-buttons interactive carries no URL button, so the link option is appended to the
    # body as a "label: url" line — surfaced, never silently dropped.
    assert marker in interactive["body"]["text"]
    assert f"Docs: {docs_url}" in interactive["body"]["text"]
    # The media header and the footer ride the same interactive frame.
    assert interactive["header"] == {"type": "image", "image": {"link": header_url}}
    assert interactive["footer"] == {"text": "powered by tai42"}


@pytest.mark.needs(
    "kind:channels:whatsapp", "helper:whatsapp", "setting:router:notifications", "setting:tool:notify_user"
)
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
                # ``parameters`` is the channel-owned opaque object; this channel reads its own
                # ``body_parameters`` shape out of it.
                "template": {"name": name, "language": "en_US", "parameters": {"body_parameters": ["Ada", "3pm"]}},
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


@pytest.mark.needs("kind:channels:twilio", "helper:twilio", "setting:router:notifications", "setting:tool:notify_user")
async def test_notify_rich_content_capability_and_channel_guards(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("l7-guard")
    media = [{"kind": "image", "url": f"https://cdn.example.com/{marker}.jpg"}]
    template = {"name": f"{marker}_t", "language": "en_US"}

    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        # Twilio advertises ``supports_media_notifications`` (real MMS/WhatsApp media), so
        # a media notify is DELIVERED — each image rides as an MMS ``MediaUrl`` attachment,
        # never a 501 and never a silent downgrade that drops the media.
        media_ok = await mcp.call_tool(
            "notify_user",
            {"message": marker, "channel": "twilio", "recipient": BRIDGE_TWILIO_CLIENT, "media": media},
            raise_on_error=False,
        )
        assert not media_ok.is_error, tool_text(media_ok)
        assert "notification sent via 'twilio'" in tool_text(media_ok)

        # Templates remain a genuinely unsupported capability on twilio (a vendor
        # WhatsApp-Cloud construct); the guard still fires -> 501, never a freeform downgrade.
        template_501 = await mcp.call_tool(
            "notify_user",
            {"message": marker, "channel": "twilio", "recipient": BRIDGE_TWILIO_CLIENT, "template": template},
            raise_on_error=False,
        )
        assert template_501.is_error
        assert "does not support template notifications" in tool_text(template_501)

        # Rich content with NO named channel is RECORDED to the internal sink (parity
        # with the channel path) — the host inbox renders the stored media/template itself.
        media_sink = await mcp.call_tool("notify_user", {"message": marker, "media": media}, raise_on_error=False)
        assert not media_sink.is_error, tool_text(media_sink)
        assert "recorded to the internal sink" in tool_text(media_sink)

        template_sink = await mcp.call_tool(
            "notify_user", {"message": marker, "template": template}, raise_on_error=False
        )
        assert not template_sink.is_error, tool_text(template_sink)
        assert "recorded to the internal sink" in tool_text(template_sink)

    # The one dispatch twilio was asked for is the supported media send, carrying the image
    # url as an MMS attachment. The refused template send and the channel-less sends left
    # nothing on the wire — their guards fired before twilio was ever asked.
    twilio_sends = bridge.fake_twilio.sends_matching(marker)
    assert len(twilio_sends) == 1
    assert twilio_sends[0]["to"] == BRIDGE_TWILIO_CLIENT
    assert twilio_sends[0]["media_urls"] == [media[0]["url"]]


@pytest.mark.needs(
    "kind:channels:whatsapp", "helper:whatsapp", "setting:router:notifications", "setting:tool:notify_user"
)
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


@pytest.mark.needs(
    "kind:channels:whatsapp", "helper:whatsapp", "setting:router:notifications", "setting:tool:notify_user"
)
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


@pytest.mark.needs(
    "kind:channels:whatsapp",
    "helper:llm",
    "helper:whatsapp",
    "setting:router:notifications",
    "setting:seeded-access-control",
    "setting:tool:notify_user",
    "topology:replicas",
)
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


_FLOW_ENCRYPTION_PATH = "/api/channels/whatsapp/flow-encryption"


@pytest.mark.needs("kind:channels:whatsapp", "setting:seeded-access-control")
async def test_flow_encryption_registers_the_public_key_for_the_default_number(bridge: BridgeHarness) -> None:
    data = await bridge.api().post(_FLOW_ENCRYPTION_PATH, json={})

    assert data["phone_number_id"] == BRIDGE_WHATSAPP_PHONE_ID
    assert "BEGIN PUBLIC KEY" in data["public_key"]
    assert bridge.fake_whatsapp.public_keys == [
        {
            "token": "e2e-whatsapp-access-token",
            "phone_number_id": BRIDGE_WHATSAPP_PHONE_ID,
            "business_public_key": data["public_key"],
        }
    ]


@pytest.mark.needs("kind:channels:whatsapp", "setting:seeded-access-control")
async def test_flow_encryption_registers_for_a_named_number(bridge: BridgeHarness) -> None:
    other = _fresh_phone_id()

    data = await bridge.api().post(_FLOW_ENCRYPTION_PATH, json={"phone_number_id": other})

    assert data["phone_number_id"] == other
    assert [record["phone_number_id"] for record in bridge.fake_whatsapp.public_keys] == [other]
    assert bridge.fake_whatsapp.public_keys[0]["business_public_key"] == data["public_key"]


@pytest.mark.needs("kind:channels:whatsapp", "setting:seeded-access-control")
async def test_flow_encryption_refuses_a_caller_without_a_key(bridge: BridgeHarness) -> None:
    anon = ApiClient(bridge.stack.origin(bridge.stack.port_b))

    refused = await anon.request_raw("POST", _FLOW_ENCRYPTION_PATH, json={})

    assert refused.status_code == 401, refused.text
    assert refused.json() == {"error": "Authentication required"}
    assert bridge.fake_whatsapp.public_keys == []


@pytest.mark.needs("kind:channels:whatsapp", "setting:seeded-access-control")
async def test_flow_encryption_refuses_a_non_admin_key(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    # The scoped key reaches every route through the path-scope table but is not an
    # administrator, so the admin-only fence refuses it.
    scoped = await bridge.mint_key(user_id=uniq("flow-enc-scoped"), scopes=["e2e-all"])

    refused = await bridge.api(token=scoped).request_raw("POST", _FLOW_ENCRYPTION_PATH, json={})

    assert refused.status_code == 403, refused.text
    assert refused.json() == {"error": "Forbidden"}
    assert bridge.fake_whatsapp.public_keys == []
