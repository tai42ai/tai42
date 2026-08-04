"""L1 — channel conversation round-trip (twilio fake, WhatsApp-shaped addresses).

An uncorrelated inbound webhook routes to the bridge, runs a deterministic agent turn, the
answer is sent back FROM the texted identity, and a second inbound on the same pair proves
the turn remembered the first — checkpoint continuity across two separate webhook fires.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    default_twilio_identity,
    post_inbound,
    request_mentions,
    script_reply,
    wait_twilio_send,
)

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings

# The scripted-LLM + FakeTwilio round-trips are the mock leg for BOTH the 'twilio' channel seam
# and the 'llm' seam (build_bridge_stack wires the LLM env too, so TAI_E2E_REAL=llm also sends
# the bridge LLM to the live provider). Either selection real breaks the stub scripting, so the
# module steps aside; the real legs run on the dedicated e2e creds host (PLAN_2 §F), not in CI.
# Inert in the default mock run — both is_real checks are False, so collection is byte-for-byte
# today's.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("llm"),
    reason="FakeTwilio + scripted-LLM is the 'twilio'/'llm' mock leg; real legs on the creds host (PLAN_2 §F)",
)


async def test_twilio_bridge_round_trip_and_checkpoint_continuity(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = default_twilio_identity()
    client = BRIDGE_TWILIO_CLIENT
    route_name = uniq("l1-route").replace("_", "-")
    execution_key = uniq("l1-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=execution_key,
        channel="twilio",
        our_identity=identity,
    )

    first_marker = uniq("remember")
    answer1 = uniq("ans1")
    answer2 = uniq("ans2")
    script_reply(bridge.llm_stub, f"Noted: {answer1}", f"Recalling {answer2}")

    # Fire 1 — uncorrelated inbound on the routed pair, both fires pinned to replica B so the
    # memory-checkpoint continuity lives in one serve worker.
    port = bridge.stack.port_b
    inbound1 = bridge.twilio_inbound(
        our_identity=identity, client=client, text=f"my secret is {first_marker}", port=port
    )
    resp1 = await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound1, port=port)
    assert resp1.status_code == 204, resp1.text

    send1 = await wait_twilio_send(bridge.fake_twilio, answer1)
    # Delivered back FROM the texted identity (the deployment number), TO the human.
    assert send1["from"] == identity
    assert send1["to"] == client

    # Fire 2 — same pair; the agent answers a question only the restored history can serve.
    inbound2 = bridge.twilio_inbound(our_identity=identity, client=client, text="what was my secret?", port=port)
    resp2 = await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound2, port=port)
    assert resp2.status_code == 204, resp2.text

    await wait_twilio_send(bridge.fake_twilio, answer2)

    # The second turn's LLM request replayed the first turn's user message — the checkpoint
    # carried the conversation across the two separate webhook fires.
    assert request_mentions(bridge.llm_stub, 1, first_marker), (
        "the second bridge turn did not see the first turn's history; checkpoint continuity broke"
    )
