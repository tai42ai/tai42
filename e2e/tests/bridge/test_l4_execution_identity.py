"""L4 — execution identity on the bridge path.

The turn runs AS the route's bound execution key, authorized live at fire against that key's
current grants:

* Automatic revocation: a bound key that runs, then is de-scoped (deleted), makes the next
  fire denied with a client-safe ``error`` outcome, not a crash.
* A non-admin caller may bind only a key it owns; binding a key it does not own is rejected
  at create, before any write.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _bridge_support import (
    ERROR_ANSWER_TEXT,
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    default_twilio_identity,
    post_inbound,
    script_reply,
    wait_twilio_send,
)

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_FROM_B
from tai42_e2e.settings import HarnessSettings

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


async def _fire(bridge: BridgeHarness, identity: str, client: str, text: str) -> None:
    port = bridge.stack.port_b
    inbound = bridge.twilio_inbound(our_identity=identity, client=client, text=text, port=port)
    resp = await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)
    assert resp.status_code == 204, resp.text


async def test_headline_automatic_revocation(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    identity = default_twilio_identity()
    client = BRIDGE_TWILIO_CLIENT
    route_name = uniq("l4-route").replace("_", "-")
    execution_key = uniq("l4-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=execution_key,
        channel="twilio",
        our_identity=identity,
    )

    # A fire under the live, authorized key runs the agent and answers.
    answer = uniq("l4-ok")
    script_reply(bridge.llm_stub, f"answered {answer}")
    await _fire(bridge, identity, client, "first question")
    await wait_twilio_send(bridge.fake_twilio, answer)

    # Revoke the execution key by deleting it — no route-side step.
    await bridge.delete_key(execution_key)

    # The next fire is denied at authorize-agent-run: no LLM call, a client-safe error answer.
    bridge.fake_twilio.reset()
    bridge.llm_stub.reset()
    await _fire(bridge, identity, client, "second question after revocation")
    send = await wait_twilio_send(bridge.fake_twilio, ERROR_ANSWER_TEXT)
    assert send["from"] == identity
    assert send["to"] == client
    # The turn never reached the agent — the seam denied it before any model call.
    assert bridge.llm_stub.requests == []


async def test_bind_a_key_you_do_not_own_is_rejected(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    # A non-admin owner key (e2e-all reaches the route door but is not the ``*`` admin scope).
    owner_id = uniq("l4-owner")
    owner_token = await bridge.mint_key(user_id=owner_id, scopes=["e2e-all"])

    # A key the owner does NOT own (minted by root, self-owned by root/admin context).
    foreign_key = uniq("l4-foreign")
    await bridge.mint_key(user_id=foreign_key, scopes=["e2e-all"])

    # Binding a key it does not own is a pass-role denial at create — a 403, no row written.
    # A second identity (not the headline leg's) so the pass-role check is what refuses it,
    # never the already-routed-identity guard the shared stack would raise first.
    await bridge.create_channel_route(
        route_name=uniq("l4-badroute").replace("_", "-"),
        agent="tools_agent",
        execution_key=foreign_key,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM_B,
        token=owner_token,
        expect=403,
    )
