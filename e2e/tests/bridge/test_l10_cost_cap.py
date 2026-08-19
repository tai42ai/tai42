"""L10 — cost caps: the per-address rate shed and the global turn ceiling.

* One ``client_address`` over its per-hour turn cap is shed with exactly one client-visible
  slow-down reply, and the over-limit message runs no turn.
* A burst of fresh addresses runs concurrent turns, but never more at once than the global
  in-flight ceiling — the fan-out is bounded, not unbounded.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.manifests import (
    BRIDGE_MAX_CONCURRENT_TURNS,
    BRIDGE_PER_ADDRESS_TURNS_PER_HOUR,
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_TWILIO_FROM_B,
)
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    SLOW_DOWN_TEXT,
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    wait_channel_send_count,
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


async def test_over_cap_address_is_shed_with_one_slow_down_reply(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = BRIDGE_TWILIO_FROM
    client = BRIDGE_TWILIO_CLIENT
    exec_key = uniq("l10a-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("l10a-route").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_name, agent="tools_agent", execution_key=exec_key, channel="twilio", our_identity=identity
    )
    port = bridge.stack.port_b

    cap = BRIDGE_PER_ADDRESS_TURNS_PER_HOUR
    marker = uniq("l10a")
    script_reply(bridge.llm_stub, *[f"ans {marker} {i}" for i in range(cap)])

    # Fire the whole bucket, one at a time so it drains in door order.
    for i in range(cap):
        inbound = bridge.twilio_inbound(our_identity=identity, client=client, text=f"msg {i}", port=port)
        assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204

    # The next message from the same pair is over the cap: shed with a slow-down reply.
    over = bridge.twilio_inbound(our_identity=identity, client=client, text="one too many", port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, over, port=port)).status_code == 204

    # Every admitted message answered; the shed one delivered exactly one slow-down reply to
    # the same client and ran no turn.
    await wait_channel_send_count(bridge.fake_twilio, marker, cap)
    slow = await wait_twilio_send(bridge.fake_twilio, SLOW_DOWN_TEXT)
    assert slow["to"] == client
    assert slow["from"] == identity
    assert len(bridge.llm_stub.requests) == cap


async def test_global_ceiling_bounds_concurrent_turns(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    identity = BRIDGE_TWILIO_FROM_B
    exec_key = uniq("l10b-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("l10b-route").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_name, agent="tools_agent", execution_key=exec_key, channel="twilio", our_identity=identity
    )
    port = bridge.stack.port_b

    burst = 3 * BRIDGE_MAX_CONCURRENT_TURNS
    marker = uniq("l10b")
    script_reply(bridge.llm_stub, *[f"burst {marker} {i}" for i in range(burst)])

    # Hold each turn's model call open so genuinely concurrent turns overlap and the stub can
    # see the peak; every fresh address gets its own full bucket, so all are admitted.
    bridge.llm_stub.set_response_delay(2.0)
    try:
        for i in range(burst):
            client = f"+1666{i:07d}"
            inbound = bridge.twilio_inbound(our_identity=identity, client=client, text="burst", port=port)
            assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204
        await wait_channel_send_count(bridge.fake_twilio, marker, burst, deadline=90.0)
    finally:
        bridge.llm_stub.set_response_delay(0.0)

    # Turns genuinely overlapped (peak > 1), but never more than the ceiling at once — a
    # broken limiter would let the whole burst run concurrently.
    peak = bridge.llm_stub.max_in_flight_completions
    assert 1 < peak <= BRIDGE_MAX_CONCURRENT_TURNS, (
        f"peak in-flight turns {peak}, ceiling {BRIDGE_MAX_CONCURRENT_TURNS}"
    )
