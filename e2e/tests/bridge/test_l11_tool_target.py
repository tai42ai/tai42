"""L11 — tool-target conversation routes (twilio fake + api door).

A ``target_kind=tool`` route dispatches a registered tool per inbound message instead of
running an agent: the message maps to the tool kwargs (``payload_expr``), the tool result
maps to the reply (``reply_expr`` or a null/string pass-through), and a reply that maps to
null/blank sends nothing at all. No scripted LLM is involved — the tool runs directly under
the route's execution key.

Silence differs by door. On the CHANNEL door a null-mapped reply is terminal ``silent`` and
nothing is ever sent. On the API door it is a durable ``silent`` marker delivered through the
same machine an answer takes: inline in a ``200`` under a sync wait, or POSTed as the signed
callback body ``{message_id, thread_id, status: "silent"}`` (no ``answer`` key) otherwise.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    wait_probe_record,
    wait_record_status,
    wait_twilio_send,
)

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_CLIENT_B,
    BRIDGE_TWILIO_FROM,
    BRIDGE_TWILIO_FROM_B,
)
from tai42_e2e.settings import HarnessSettings

# An https callback URL with no server behind it — the delivery POST connection-refuses, so
# an api-door async record exhausts its (shortened) attempts and lands terminal ``failed``.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


@pytest.mark.skipif(
    HarnessSettings().is_real("twilio"),
    reason="FakeTwilio is the 'twilio' mock leg; the real leg runs on the creds host",
)
async def test_tool_target_echoes_a_reply_and_maps_null_to_silence(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    exec_echo = uniq("l11-exec-echo")
    exec_silent = uniq("l11-exec-silent")
    await bridge.mint_key(user_id=exec_echo, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_silent, scopes=["e2e-all"])
    route_echo = uniq("l11-route-echo").replace("_", "-")
    route_silent = uniq("l11-route-silent").replace("_", "-")

    # The echo route maps the message to e2e_echo(payload=...) and passes its string result
    # straight back as the reply.
    await bridge.create_tool_channel_route(
        route_name=route_echo,
        tool="e2e_echo",
        execution_key=exec_echo,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        payload_expr="{payload: .message}",
    )
    # The silent route runs e2e_record — a tool whose Redis side effect proves it EXECUTED —
    # but maps its reply to null, so nothing is ever sent. The side effect is the completion
    # barrier a silent turn otherwise lacks (no outbound send, and a channel record carries no
    # id the harness can read back).
    silent_probe = uniq("l11-silent-ran")
    await bridge.create_tool_channel_route(
        route_name=route_silent,
        tool="e2e_record",
        execution_key=exec_silent,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM_B,
        payload_expr=f'{{key: "{silent_probe}", value: .message}}',
        reply_expr="null",
    )

    marker_echo = uniq("l11-echo")
    marker_silent = uniq("l11-silent")
    port = bridge.stack.port_b

    # Fire the silent-route inbound first, then the echo-route inbound.
    inbound_silent = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM_B, client=BRIDGE_TWILIO_CLIENT_B, text=marker_silent, port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound_silent, port=port)).status_code == 204

    inbound_echo = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text=marker_echo, port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound_echo, port=port)).status_code == 204

    # The tool echoed the inbound message verbatim, delivered FROM the texted identity.
    send = await wait_twilio_send(bridge.fake_twilio, marker_echo)
    assert send["from"] == BRIDGE_TWILIO_FROM
    assert send["to"] == BRIDGE_TWILIO_CLIENT
    assert send["body"] == marker_echo

    # Barrier: the silent turn's tool ran to completion (its side effect landed). The echo
    # turn — fired after and carrying a full outbound send round-trip — has also settled, so
    # any errant send the silent turn produced would already be recorded.
    recorded = await wait_probe_record(bridge, silent_probe)
    assert [entry["value"] for entry in recorded] == [marker_silent]

    # The silent route sent NOTHING: no outbound touching its conversation at all — not marker
    # matching, but ANY send from identity B or to client B, so a blank or error-text send is
    # caught too.
    stray = [
        message
        for message in bridge.fake_twilio.messages
        if message["from"] == BRIDGE_TWILIO_FROM_B or message["to"] == BRIDGE_TWILIO_CLIENT_B
    ]
    assert stray == [], f"the silent tool route sent {stray!r}; a null-mapped reply must send nothing"


async def test_api_tool_target_null_reply_answers_silent_inline(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """The api door's sync wait: a tool turn mapping its reply to null finishes inside the
    wait and answers ``200`` with the silent marker inline — ``{message_id, thread_id,
    status: "silent"}`` and NO ``answer`` key, the exact ``ConversationAnswer`` shape the
    signed callback body also carries."""
    exec_key = uniq("l11-api-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route = uniq("l11-api-silent").replace("_", "-")
    await bridge.create_tool_api_route(
        route_name=route,
        tool="e2e_echo",
        execution_key=exec_key,
        callback_url=_UNREACHABLE_CALLBACK,
        payload_expr="{payload: .message}",
        reply_expr="null",
    )

    caller = await bridge.mint_key(user_id=uniq("l11-api-caller"), scopes=["e2e-all"])
    data = await bridge.api(token=caller).post(
        f"/api/conversations/{route}/messages",
        json={"external_user_id": uniq("l11-api-user"), "text": "ping", "wait_seconds": 20},
        expect=200,
    )
    answer = data["answer"]
    assert answer["status"] == "silent"
    assert answer["message_id"] == data["message_id"]
    assert answer["thread_id"] == data["thread_id"]
    assert "answer" not in answer, f"a silent answer carries no answer key, got {answer!r}"

    # Settled delivered inline; the unreachable callback was suppressed, never POSTed.
    record = await bridge.get_record(route, data["message_id"])
    assert record["answer_status"] == "silent"
    assert record["answer"] is None
    assert record["delivery_status"] == "delivered"


async def test_api_tool_target_null_reply_delivers_silent_marker_async(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """The api door's async path: a null-mapped tool reply is NOT terminal like the channel
    door's — it is a durable ``silent`` marker driven through the delivery machine. With no
    sync wait the door answers ``202`` and the signed callback (carrying ``status: "silent"``,
    no answer) POSTs to the unreachable https sink until it exhausts its attempts → terminal
    ``failed``, proving the marker travelled the durable pipeline rather than being dropped."""
    exec_key = uniq("l11-api-exec2")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route = uniq("l11-api-silent2").replace("_", "-")
    await bridge.create_tool_api_route(
        route_name=route,
        tool="e2e_echo",
        execution_key=exec_key,
        callback_url=_UNREACHABLE_CALLBACK,
        payload_expr="{payload: .message}",
        reply_expr="null",
    )

    caller = await bridge.mint_key(user_id=uniq("l11-api-caller2"), scopes=["e2e-all"])
    accepted = await bridge.api(token=caller).post(
        f"/api/conversations/{route}/messages",
        json={"external_user_id": uniq("l11-api-user2"), "text": "ping"},
        expect=202,
    )
    assert "answer" not in accepted, f"the async 202 carries only the ids, got {accepted!r}"
    message_id = accepted["message_id"]

    record = await wait_record_status(bridge, route, message_id, {"failed"}, deadline=20.0)
    assert record["answer_status"] == "silent"
    assert record["answer"] is None

    failed = await bridge.list_failed()
    assert any(item["message_id"] == message_id for item in failed["items"])
