"""L2 — precedence: a pending ask_user question wins over a bridge turn.

With a pending ask_user on a routed number pair, the human's reply resolves the ASK
(existing correlation behavior), NOT a bridge turn; a follow-up uncorrelated message on the
same pair then starts a bridge turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from _bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    cancel_and_join,
    default_twilio_identity,
    post_inbound,
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


async def test_pending_ask_resolves_then_uncorrelated_starts_a_turn(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = default_twilio_identity()
    client = BRIDGE_TWILIO_CLIENT
    route_name = uniq("l2-route").replace("_", "-")
    execution_key = uniq("l2-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=execution_key,
        channel="twilio",
        our_identity=identity,
    )

    question = uniq("l2-q")
    ask_answer = uniq("l2-ans")

    async def ask() -> object:
        # ask_user delivers over twilio to the operator default recipient (the same human)
        # from the deployment number — a Tier-2 pending correlation on that pair.
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool("ask_user", {"question": question, "channel": "twilio"})
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        await wait_twilio_send(bridge.fake_twilio, question)
        # The reply is a correlation HIT — it resolves the pending ASK, never the bridge.
        reply = bridge.twilio_inbound(our_identity=identity, client=client, text=ask_answer, port=bridge.stack.port_b)
        resp = await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, reply, port=bridge.stack.port_b)
        assert resp.status_code == 204, resp.text
        resolved = await asyncio.wait_for(ask_task, timeout=15.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == ask_answer
    # No bridge turn ran: the ask is a direct tool call, so the scripted LLM was never asked.
    assert bridge.llm_stub.requests == []

    # A follow-up uncorrelated message on the same pair (no pending now) DOES start a turn.
    bridge.fake_twilio.reset()
    bridge.llm_stub.reset()
    bridge_answer = uniq("l2-bridge")
    script_reply(bridge.llm_stub, f"bridged {bridge_answer}")
    follow_up = bridge.twilio_inbound(
        our_identity=identity, client=client, text="a brand new question", port=bridge.stack.port_b
    )
    resp2 = await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, follow_up, port=bridge.stack.port_b)
    assert resp2.status_code == 204, resp2.text

    send = await wait_twilio_send(bridge.fake_twilio, bridge_answer)
    assert send["from"] == identity
    assert send["to"] == client
    assert len(bridge.llm_stub.requests) == 1
