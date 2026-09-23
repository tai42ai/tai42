"""The cross-channel pairing handshake over two channels into one target.

One multichannel target reachable on two channel routes (a FakeTwilio route and a
FakeWhatsApp route). A ``/link`` typed on the twilio side runs a pairing turn that mints a
fresh ``LINK-`` code and answers it back ON twilio; that same code typed from the whatsapp
side redeems into a merge and answers ``linked`` ON whatsapp. Each reply stays on the
channel its inbound arrived on — the two conversations are answered independently even as
the platform folds their two addresses into one person.

The target here is a TOOL (an echo-style e2e tool); this leg pins the HANDSHAKE and the
per-channel reply isolation — the mint, the neutral code reply, the redeem, the linked reply,
and that each reply stays on its own channel. The merged-person's behavioral surface is pinned
elsewhere: tool-target continuity (a tool keying its state on the survivor ``person_id`` across
both channels) by ``test_tool_person_continuity``, agent memory continuity by ``test_agent_thread_continuity``.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
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

# The whole leg is the mock leg for both channel seams: it drives FakeTwilio and FakeWhatsApp
# signed inbound and reads their in-process sends. No LLM turn runs (a tool target dispatches
# directly, and the pairing turns never reach a target), so the 'llm' seam is not exercised.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp"),
    reason="FakeTwilio + FakeWhatsApp are the 'twilio'/'whatsapp' mock leg; real on the creds host",
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
        start_expr="{payload: .message}",
    )
    await bridge.create_tool_channel_route(
        route_name=route_wa,
        tool=_TOOL,
        execution_key=exec_wa,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
        start_expr="{payload: .message}",
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


async def test_a_park_survives_a_mid_park_merge_and_resumes_under_the_merged_subject(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    # One multichannel tool target (a parking tool-target) reachable on a twilio route and a
    # whatsapp route. A message on twilio parks a run under the twilio person; the two channels then
    # PAIR mid-park, folding both addresses into one survivor person and re-keying the park onto the
    # merged subject. Answering the park out of band delivers its resumed reply back to the
    # originating channel EXACTLY ONCE — proof the re-key kept the park (once, not per-identity) and
    # it resumes under the merged subject.
    port = bridge.stack.port_b
    tool = "e2e_caller_hold"
    # The door contract: start a held caller ask while nothing is parked; surface the ask's question
    # while it is pending; otherwise resume the pending ask with the inbound message, and map the
    # resumed run's terminal (``.answer``) to the reply. Its resumer RETURNS its terminal — the
    # platform's delivery ladder maps it, never a raw address-tool call.
    start_expr = "if ($parked | length) > 0 then null else {question: .message, expiry_seconds: 3600} end"
    reply_expr = "if ($asks | length) > 0 then $asks[0].question else .answer end"
    resume_expr = "if ($parked | length) > 0 then {id: $parked[0].id, payload: .message} else null end"
    await bridge.set_target_config(target_kind="tool", target_name=tool, multichannel=True)
    exec_tw = uniq("l17-exec-tw")
    exec_wa = uniq("l17-exec-wa")
    await bridge.mint_key(user_id=exec_tw, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_wa, scopes=["e2e-all"])
    route_tw = uniq("l17-route-tw").replace("_", "-")
    route_wa = uniq("l17-route-wa").replace("_", "-")
    # Distinct deployment identities per run — the module stack is shared, so a fixed pair would
    # route this spec's inbound to another spec's route on the same channel address.
    ident_tw = "+1666" + "".join(c for c in uniq("itw") if c.isdigit()).ljust(7, "0")[:7]
    ident_wa = "1666" + "".join(c for c in uniq("iwa") if c.isdigit()).ljust(8, "0")[:8]
    for route_name, execution_key, channel, our_identity in (
        (route_tw, exec_tw, "twilio", ident_tw),
        (route_wa, exec_wa, "whatsapp", ident_wa),
    ):
        await bridge.api().post(
            f"/api/conversations/{route_name}",
            json={
                "door": "channel",
                "target_kind": "tool",
                "target_name": tool,
                "execution_key": execution_key,
                "channel": channel,
                "our_identity": our_identity,
                "start_expr": {"content": start_expr},
                "reply_expr": {"content": reply_expr},
                "resume_expr": {"content": resume_expr},
            },
            expect=200,
        )

    # Distinct client addresses per run — the module stack is shared, so a fixed pair would carry a
    # previous spec's merge into this one.
    tw_client = "+1555" + "".join(c for c in uniq("tw") if c.isdigit()).ljust(7, "0")[:7]
    wa_client = "1555" + "".join(c for c in uniq("wa") if c.isdigit()).ljust(8, "0")[:8]

    # A message on twilio parks a caller ask under the twilio person; its question surfaces on twilio.
    question = uniq("l17-question")
    park_inbound = bridge.twilio_inbound(our_identity=ident_tw, client=tw_client, text=question, port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, park_inbound, port=port)).status_code == 204
    await wait_send_to(bridge.fake_twilio, to=tw_client, needle=question, deadline=20.0)

    # Pair the two channels MID-PARK: /link on twilio mints a code, redeemed on whatsapp into a merge.
    link_inbound = bridge.twilio_inbound(our_identity=ident_tw, client=tw_client, text="/link", port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, link_inbound, port=port)).status_code == 204
    (link_reply,) = await wait_send_to(bridge.fake_twilio, to=tw_client, needle=LINK_REPLY_PREFIX)
    code = extract_pair_code(link_reply["body"])
    redeem_inbound = bridge.whatsapp_inbound(phone_number_id=ident_wa, wa_id=wa_client, text=code)
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, redeem_inbound, port=port)).status_code == 200
    (linked_reply,) = await wait_send_to(bridge.fake_whatsapp, to=wa_client, needle=LINKED_TEXT)
    assert linked_reply["body"] == LINKED_TEXT

    # Resume the park from the OTHER door: a message on whatsapp finds the ONE re-keyed park under the
    # merged subject and resumes it; the resumed terminal maps through reply_expr to a whatsapp reply.
    answer = uniq("l17-answer")
    resume_inbound = bridge.whatsapp_inbound(phone_number_id=ident_wa, wa_id=wa_client, text=answer)
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, resume_inbound, port=port)).status_code == 200
    (reply,) = await wait_send_to(bridge.fake_whatsapp, to=wa_client, needle=answer, deadline=30.0)
    assert answer in reply["body"]
    # Exactly once — the re-key kept ONE park under the merged subject, not a copy per pre-merge identity.
    assert len(bridge.fake_whatsapp.sends_matching(answer)) == 1
