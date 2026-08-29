"""L18 — the first-contact greeting: delivered once as its own leading message, and its
``{pairing_code}`` is live.

A greeting-configured target. The FIRST admitted inbound from an address is answered with
the configured greeting delivered as its OWN LEADING message (a separate send), FOLLOWED by
the turn's answer as the next send — the greeting is a message of its own, no longer text
prepended into the one answer. The record still carries both joined (greeting + blank line +
answer) as its whole ``answer`` text, so a transcript reader reads the turn as one answer. A
SECOND inbound from that address carries no greeting — first contact is the person-row
creation, and the row now exists. When the template references ``{pairing_code}``, a fresh
code is minted at greeting time and rendered into the greeting message; that code is a real,
live pair code — redeeming it from another channel links the two (a greeting code can chain
another channel).
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_CLIENT_B,
    BRIDGE_TWILIO_FROM,
    BRIDGE_TWILIO_FROM_B,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    LINKED_TEXT,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    extract_pair_code,
    post_inbound,
    script_reply,
    wait_send_to,
)

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm") or HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp"),
    reason="scripted-LLM + channel stubs are the 'llm'/'twilio'/'whatsapp' mock leg; real on creds host",
)

_AGENT = "tools_agent"


async def test_greeting_leads_once_and_its_pairing_code_is_redeemable(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port = bridge.stack.port_b

    greet = uniq("l18-greet")
    # A template that references {pairing_code}: a fresh code is minted at greeting time and
    # rendered in. The greeting text itself carries a per-test marker so it is identifiable.
    await bridge.set_target_config(
        target_kind="agent", target_name=_AGENT, multichannel=True, greeting_template=f"{greet} {{pairing_code}}"
    )
    exec_tw = uniq("l18-exec-tw")
    exec_wa = uniq("l18-exec-wa")
    await bridge.mint_key(user_id=exec_tw, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_wa, scopes=["e2e-all"])
    route_tw = uniq("l18-route-tw").replace("_", "-")
    route_wa = uniq("l18-route-wa").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_tw, agent=_AGENT, execution_key=exec_tw, channel="twilio", our_identity=BRIDGE_TWILIO_FROM
    )
    await bridge.create_channel_route(
        route_name=route_wa,
        agent=_AGENT,
        execution_key=exec_wa,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
    )

    answer1 = uniq("l18-ans1")
    answer2 = uniq("l18-ans2")
    script_reply(bridge.llm_stub, answer1, answer2)

    # First inbound: the greeting leads as its OWN send, then the answer follows as the next
    # send. The greeting message is exactly ``{greet} {code}`` and the second send is the bare
    # answer — no longer one message with the greeting prepended.
    first = bridge.twilio_inbound(our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="hello", port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, first, port=port)).status_code == 204
    greeting_send, answer_send = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, count=2)
    assert greeting_send["body"].startswith(f"{greet} "), greeting_send["body"]
    code = extract_pair_code(greeting_send["body"])
    assert greeting_send["body"] == f"{greet} {code}", greeting_send["body"]
    assert answer_send["body"] == answer1, answer_send["body"]

    # The record still carries both joined as its whole answer text — the greeting, a blank
    # line, then the answer — so a transcript reads the turn as one answer even though it was
    # delivered as two messages.
    thread_id = (await bridge.api().get(f"/api/conversations/{route_tw}/threads"))["items"][0]["thread_id"]
    transcript = await bridge.api().get(
        f"/api/conversations/{route_tw}/transcript?{urlencode({'thread_id': thread_id})}"
    )
    assert transcript["items"][0]["answer"] == f"{greet} {code}\n\n{answer1}", transcript["items"][0]

    # Second inbound from the SAME address: no greeting, no code — just the answer, and only
    # one send (no leading greeting message this time).
    second = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="hello again", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, second, port=port)).status_code == 204
    sends = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, count=3)
    assert sends[2]["body"] == answer2, sends[2]["body"]
    # The greeting was delivered EXACTLY ONCE across both turns — the once-only invariant.
    assert sum(1 for send in sends if send["body"].startswith(f"{greet} ")) == 1, [s["body"] for s in sends]
    assert "LINK-" not in sends[2]["body"]

    # The greeting's minted code is a real, live pair code: redeeming it from whatsapp links
    # the two conversations. The whatsapp side is itself a first contact, so it ALSO fires its
    # greeting — delivered here as its own leading message (with a fresh code of its own that
    # could chain a third channel), FOLLOWED by the ``linked`` reply as the next send.
    redeem = bridge.whatsapp_inbound(phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=code)
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, redeem, port=port)).status_code == 200
    wa_greeting, wa_linked = await wait_send_to(bridge.fake_whatsapp, to=BRIDGE_WHATSAPP_CLIENT, count=2)
    assert wa_greeting["body"].startswith(f"{greet} "), wa_greeting["body"]
    # The whatsapp greeting minted its OWN fresh code, not the redeemed one.
    assert extract_pair_code(wa_greeting["body"]) != code, wa_greeting["body"]
    assert wa_linked["body"] == LINKED_TEXT, wa_linked["body"]


async def test_greeting_leads_once_on_a_tool_target(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """The greeting path is target-agnostic: a greeting-configured TOOL target delivers the
    greeting as its OWN leading message on first contact, then the tool reply as the next
    send, and the address's second message carries no greeting. The template is a fixed string
    (no ``{pairing_code}``), so the first send is exactly ``{greet}`` and the second the bare
    tool reply; the record still stores both joined as ``{greet}\\n\\n{tool reply}``."""
    port = bridge.stack.port_b

    greet = uniq("l18-tool-greet")
    await bridge.set_target_config(
        target_kind="tool", target_name="e2e_echo", multichannel=True, greeting_template=greet
    )
    exec_key = uniq("l18-tool-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route = uniq("l18-tool-route").replace("_", "-")
    # A distinct deployment number from the agent leg's: one ``(channel, our_identity)`` pair
    # routes to exactly one route, and the two tests share this module's stack.
    await bridge.create_tool_channel_route(
        route_name=route,
        tool="e2e_echo",
        execution_key=exec_key,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM_B,
        payload_expr="{payload: .message}",
    )

    # First contact: the greeting leads as its own send, then the tool echo as the next send.
    first = uniq("l18-tool-msg1")
    inbound1 = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM_B, client=BRIDGE_TWILIO_CLIENT_B, text=first, port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound1, port=port)).status_code == 204
    greeting_send, answer_send = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT_B, count=2)
    assert greeting_send["body"] == greet, greeting_send["body"]
    assert answer_send["body"] == first, answer_send["body"]

    # The record still carries both joined as its whole answer text (greeting, blank line,
    # then the tool reply) — the transcript reads the turn as one answer.
    thread_id = (await bridge.api().get(f"/api/conversations/{route}/threads"))["items"][0]["thread_id"]
    transcript = await bridge.api().get(f"/api/conversations/{route}/transcript?{urlencode({'thread_id': thread_id})}")
    assert transcript["items"][0]["answer"] == f"{greet}\n\n{first}", transcript["items"][0]

    # Second message from the SAME address: no greeting — just the tool reply, one send.
    second = uniq("l18-tool-msg2")
    inbound2 = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM_B, client=BRIDGE_TWILIO_CLIENT_B, text=second, port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound2, port=port)).status_code == 204
    sends = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT_B, count=3)
    assert sends[2]["body"] == second, sends[2]["body"]
    # The greeting was delivered EXACTLY ONCE across both turns — the once-only invariant.
    assert sum(1 for send in sends if send["body"] == greet) == 1, [s["body"] for s in sends]
