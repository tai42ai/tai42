"""L6 — multi-identity: two routed numbers under one fake twilio account.

Each deployment number is its own conversation route; an inbound to one fires that route's
turn and the answer leaves FROM that number, never the other's.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_CLIENT_B,
    BRIDGE_TWILIO_FROM,
    BRIDGE_TWILIO_FROM_B,
)
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    wait_twilio_send,
)

# The scripted-LLM + FakeTwilio round-trips are the mock leg for BOTH the 'twilio' channel seam
# and the 'llm' seam (build_bridge_stack wires the LLM env too, so TAI_E2E_REAL=llm also sends
# the bridge LLM to the live provider). Either selection real breaks the stub scripting, so the
# module steps aside; the real legs run on the dedicated e2e creds host, not in CI.
# Inert in the default mock run — both is_real checks are False, so collection is byte-for-byte
# today's.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("llm"),
    reason="FakeTwilio + scripted-LLM is the 'twilio'/'llm' mock leg; real legs on the creds host",
)


async def test_two_numbers_each_reply_from_their_own_identity(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    exec_a = uniq("l6-exec-a")
    exec_b = uniq("l6-exec-b")
    await bridge.mint_key(user_id=exec_a, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_b, scopes=["e2e-all"])
    route_a = uniq("l6-route-a").replace("_", "-")
    route_b = uniq("l6-route-b").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_a, agent="tools_agent", execution_key=exec_a, channel="twilio", our_identity=BRIDGE_TWILIO_FROM
    )
    await bridge.create_channel_route(
        route_name=route_b,
        agent="tools_agent",
        execution_key=exec_b,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM_B,
    )

    answer_a = uniq("l6-a")
    answer_b = uniq("l6-b")
    script_reply(bridge.llm_stub, f"from-a {answer_a}", f"from-b {answer_b}")
    port = bridge.stack.port_b

    inbound_a = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="to number A", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound_a, port=port)).status_code == 204
    send_a = await wait_twilio_send(bridge.fake_twilio, answer_a)
    assert send_a["from"] == BRIDGE_TWILIO_FROM
    assert send_a["to"] == BRIDGE_TWILIO_CLIENT

    inbound_b = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM_B, client=BRIDGE_TWILIO_CLIENT_B, text="to number B", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound_b, port=port)).status_code == 204
    send_b = await wait_twilio_send(bridge.fake_twilio, answer_b)
    assert send_b["from"] == BRIDGE_TWILIO_FROM_B
    assert send_b["to"] == BRIDGE_TWILIO_CLIENT_B
