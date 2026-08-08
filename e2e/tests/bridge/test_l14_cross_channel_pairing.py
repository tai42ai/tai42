"""L14 — the cross-channel pairing handshake over two channels into one target.

One multichannel target reachable on two channel routes (a FakeTwilio route and a
FakeWhatsApp route). A ``/link`` typed on the twilio side runs a pairing turn that mints a
fresh ``LINK-`` code and answers it back ON twilio; that same code typed from the whatsapp
side redeems into a merge and answers ``linked`` ON whatsapp. Each reply stays on the
channel its inbound arrived on — the two conversations are answered independently even as
the platform folds their two addresses into one person.

The target here is a TOOL (an echo-style e2e tool), which has no thread, so "one person" has
no behavioral surface to read (D3); this leg pins the HANDSHAKE only — the mint, the neutral
code reply, the redeem, the linked reply, and the per-channel isolation. The merged-person
proof lives in the person store's own suite and in L16 (agent memory).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _bridge_support import (
    INVALID_CODE_TEXT,
    LINK_REPLY_PREFIX,
    LINKED_TEXT,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    extract_pair_code,
    post_inbound,
    wait_send_to,
)

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings

# The whole leg is the mock leg for both channel seams: it drives FakeTwilio and FakeWhatsApp
# signed inbound and reads their in-process sends. No LLM turn runs (a tool target dispatches
# directly, and the pairing turns never reach a target), so the 'llm' seam is not exercised.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp"),
    reason="FakeTwilio + FakeWhatsApp are the 'twilio'/'whatsapp' mock leg; real on the creds host (PLAN_2 §F)",
)

_TOOL = "e2e_echo"


async def test_link_on_one_channel_redeems_on_the_other(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    port = bridge.stack.port_b

    # One tool target, multichannel ON, reachable on a twilio route and a whatsapp route.
    await bridge.set_target_config(target_kind="tool", target_name=_TOOL, multichannel=True)
    exec_tw = uniq("l14-exec-tw")
    exec_wa = uniq("l14-exec-wa")
    await bridge.mint_key(user_id=exec_tw, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_wa, scopes=["e2e-all"])
    route_tw = uniq("l14-route-tw").replace("_", "-")
    route_wa = uniq("l14-route-wa").replace("_", "-")
    await bridge.create_tool_channel_route(
        route_name=route_tw,
        tool=_TOOL,
        execution_key=exec_tw,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        payload_expr="{payload: .message}",
    )
    await bridge.create_tool_channel_route(
        route_name=route_wa,
        tool=_TOOL,
        execution_key=exec_wa,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
        payload_expr="{payload: .message}",
    )

    # ``/link`` on twilio → a pairing turn mints a fresh code and answers it back on twilio.
    link_inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="/link", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, link_inbound, port=port)).status_code == 204
    (link_reply,) = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, needle=LINK_REPLY_PREFIX)
    assert link_reply["from"] == BRIDGE_TWILIO_FROM
    code = extract_pair_code(link_reply["body"])

    # That same code typed from whatsapp redeems into a merge and answers ``linked`` on whatsapp.
    redeem_inbound = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=code
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, redeem_inbound, port=port)).status_code == 200
    (linked_reply,) = await wait_send_to(bridge.fake_whatsapp, to=BRIDGE_WHATSAPP_CLIENT, needle=LINKED_TEXT)
    assert linked_reply["body"] == LINKED_TEXT

    # Each reply stayed on its own channel: the code reply never crossed to whatsapp, the
    # linked reply never crossed to twilio, and neither channel saw the other's outcome.
    assert bridge.fake_whatsapp.sends_matching(LINK_REPLY_PREFIX) == []
    assert bridge.fake_twilio.sends_matching(LINKED_TEXT) == []
    # And no redeem error surfaced on either side — the handshake was clean, not a swallowed
    # invalid-code path answered as success.
    assert bridge.fake_twilio.sends_matching(INVALID_CODE_TEXT) == []
    assert bridge.fake_whatsapp.sends_matching(INVALID_CODE_TEXT) == []
