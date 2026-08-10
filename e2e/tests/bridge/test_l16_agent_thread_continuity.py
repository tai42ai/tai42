"""L16 — agent-thread continuity across a linked person's two channels.

An AGENT target reachable on two channel routes (twilio + whatsapp). Before pairing, the two
channels are two separate route-keyed threads: a fact stated on one is invisible to a turn on
the other. After the person links the two channels by pair code, both channels key the SAME
aggregated ``bridge:@person:{id}`` thread, so a fact stated on one channel is in the
history the other channel's next turn runs against.

The agent runs on the scripted LLM stub, so a turn's ANSWER is fixed regardless of history;
the proof that a turn SAW the history is the request the turn sent the model — the same
checkpoint-continuity read L12 uses. Histories are never migrated, so linked memory starts at
the pairing moment: the pre-pairing fact is the negative, the post-pairing fact is the
positive.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _bridge_support import (
    LINK_REPLY_PREFIX,
    LINKED_TEXT,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    extract_pair_code,
    post_inbound,
    request_mentions,
    script_reply,
    wait_send_to,
    wait_twilio_send,
    wait_whatsapp_send,
)

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings

# Scripted-LLM turns are the 'llm' mock leg; the twilio + whatsapp signed inbound the
# 'twilio'/'whatsapp' one. Any real breaks the scripting or the stubs.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm") or HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp"),
    reason="scripted-LLM + channel stubs are the 'llm'/'twilio'/'whatsapp' mock leg; real on creds host",
)

_AGENT = "tools_agent"


async def test_a_fact_on_one_channel_is_known_on_the_other_only_after_pairing(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port = bridge.stack.port_b
    await bridge.set_target_config(target_kind="agent", target_name=_AGENT, multichannel=True)
    exec_a = uniq("l16-exec-a")
    exec_b = uniq("l16-exec-b")
    await bridge.mint_key(user_id=exec_a, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_b, scopes=["e2e-all"])
    route_a = uniq("l16-route-a").replace("_", "-")
    route_b = uniq("l16-route-b").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_a, agent=_AGENT, execution_key=exec_a, channel="twilio", our_identity=BRIDGE_TWILIO_FROM
    )
    await bridge.create_channel_route(
        route_name=route_b,
        agent=_AGENT,
        execution_key=exec_b,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
    )

    pre_fact = uniq("l16-prefact")
    post_fact = uniq("l16-postfact")
    # Four AGENT turns consume one scripted answer each, in order (the pairing turns between
    # them run no model turn): A states the pre-fact, B asks, A states the post-fact, B asks.
    ans_a1 = uniq("l16-a1")
    ans_b1 = uniq("l16-b1")
    ans_a2 = uniq("l16-a2")
    ans_b2 = uniq("l16-b2")
    script_reply(bridge.llm_stub, ans_a1, ans_b1, ans_a2, ans_b2)

    async def twilio_turn(text: str, answer: str) -> None:
        inbound = bridge.twilio_inbound(
            our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text=text, port=port
        )
        assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204
        await wait_twilio_send(bridge.fake_twilio, answer)

    async def whatsapp_turn(text: str, answer: str) -> None:
        inbound = bridge.whatsapp_inbound(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=text
        )
        assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound, port=port)).status_code == 200
        await wait_whatsapp_send(bridge.fake_whatsapp, answer)

    # BEFORE pairing: A states a fact (LLM request 0), B asks (LLM request 1). B's turn runs
    # on its own route-keyed thread, so the fact stated on A is NOT in its request.
    await twilio_turn(f"my secret is {pre_fact}", ans_a1)
    await whatsapp_turn("what is my secret?", ans_b1)
    assert not request_mentions(bridge.llm_stub, 1, pre_fact), (
        "the pre-pairing whatsapp turn saw the twilio thread's fact; the two channels are not isolated"
    )

    # Pair the two channels: /link on twilio mints a code, redeemed from whatsapp → linked.
    link_inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="/link", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, link_inbound, port=port)).status_code == 204
    (link_reply,) = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, needle=LINK_REPLY_PREFIX)
    code = extract_pair_code(link_reply["body"])
    redeem_inbound = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=code
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, redeem_inbound, port=port)).status_code == 200
    await wait_send_to(bridge.fake_whatsapp, to=BRIDGE_WHATSAPP_CLIENT, needle=LINKED_TEXT)

    # AFTER pairing: A states a new fact (LLM request 2) on the now-shared person thread, then
    # B asks (LLM request 3) — and B's request DOES carry the fact A just stated. The two
    # channels aggregate to one history from the pairing moment forward.
    await twilio_turn(f"remember that {post_fact}", ans_a2)
    await whatsapp_turn("what should you remember?", ans_b2)
    assert request_mentions(bridge.llm_stub, 3, post_fact), (
        "the post-pairing whatsapp turn did not see the twilio thread's fact; the person thread did not aggregate"
    )
    # The linked memory starts at pairing: the PRE-pairing fact was never migrated into the
    # person thread, so B's post-pairing request still does not carry it.
    assert not request_mentions(bridge.llm_stub, 3, pre_fact), (
        "the pre-pairing fact leaked into the person thread; histories must not be migrated at link time"
    )
