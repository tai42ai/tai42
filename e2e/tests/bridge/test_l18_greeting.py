"""L18 — the first-contact greeting: prepended once, and its ``{pairing_code}`` is live.

A greeting-configured AGENT target. The FIRST admitted inbound from an address gets the
configured greeting PREPENDED into that same turn's delivered answer (D7 — there is no
separate greeting chunk; the greeting is the opening of the one answer). A SECOND inbound
from that address carries no greeting — first contact is the person-row creation, and the row
now exists. When the template references ``{pairing_code}``, a fresh code is minted at
greeting time and rendered into the greeting; that code is a real, live pair code — redeeming
it from another channel links the two (R15: a greeting code can chain another channel).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _bridge_support import (
    LINKED_TEXT,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    extract_pair_code,
    post_inbound,
    script_reply,
    wait_send_to,
    wait_twilio_send,
)

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm") or HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp"),
    reason="scripted-LLM + channel stubs are the 'llm'/'twilio'/'whatsapp' mock leg; real on creds host (PLAN_2 §F)",
)

_AGENT = "tools_agent"


async def test_greeting_prepends_once_and_its_pairing_code_is_redeemable(
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

    # First inbound: the greeting is PREPENDED into the same delivered answer. The rendered
    # greeting is exactly ``{greet} {code}`` and the answer follows it after a blank line.
    first = bridge.twilio_inbound(our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="hello", port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, first, port=port)).status_code == 204
    send1 = await wait_twilio_send(bridge.fake_twilio, answer1)
    assert send1["body"].startswith(f"{greet} "), send1["body"]
    code = extract_pair_code(send1["body"])
    assert send1["body"] == f"{greet} {code}\n\n{answer1}", send1["body"]

    # Second inbound from the SAME address: no greeting, no code — just the answer.
    second = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="hello again", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, second, port=port)).status_code == 204
    send2 = await wait_twilio_send(bridge.fake_twilio, answer2)
    assert send2["body"] == answer2, send2["body"]
    assert not send2["body"].startswith(greet)
    assert "LINK-" not in send2["body"]

    # The greeting's minted code is a real, live pair code: redeeming it from whatsapp links
    # the two conversations. The whatsapp side is itself a first contact, so R15 also fires its
    # greeting here — the redeem answer is the greeting PLUS ``linked``, with a fresh code of
    # its own (which could chain a third channel). So the reply ENDS WITH the linked text.
    redeem = bridge.whatsapp_inbound(phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=code)
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, redeem, port=port)).status_code == 200
    (linked_reply,) = await wait_send_to(bridge.fake_whatsapp, to=BRIDGE_WHATSAPP_CLIENT, needle=LINKED_TEXT)
    assert linked_reply["body"].endswith(LINKED_TEXT), linked_reply["body"]
    assert linked_reply["body"].startswith(f"{greet} "), linked_reply["body"]
    # The whatsapp greeting minted its OWN fresh code, not the redeemed one.
    assert extract_pair_code(linked_reply["body"]) != code, linked_reply["body"]
