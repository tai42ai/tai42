"""L21 — tool-target person continuity across a linked person's two channels.

One TOOL target (``e2e_record``) reachable on two channel routes (twilio + whatsapp), keyed on
``person_id``: ``payload_expr`` maps each inbound to ``{key: person_id, value: message}``, so
the tool writes its flow state under the person the turn ran as. A multichannel tool target's
payload carries ``person_id`` (stable from first contact; the merge survivor's id after a
``/link``), so the probe list keyed on it IS the flow state the second channel joins.

Before pairing the two channels are two provisional persons — two different keys. A ``/link``
on twilio mints a code that redeems from whatsapp into a merge; after it, a message from EITHER
channel writes under the SAME key (the survivor, one of the two pre-link ids), and those turns
key the aggregated ``bridge:@person:{id}`` thread. A ``/unlink`` from whatsapp detaches it: its
next message writes under a fresh key while twilio still writes under the survivor.

The target is silent (``reply_expr`` maps to null), so a normal turn sends nothing — its
completion barrier is the probe write, read back by the key the turn minted. The pairing turns
(``/link``, redeem, ``/unlink``) answer their fixed replies regardless, on the channel each
arrived on.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    LINK_REPLY_PREFIX,
    LINKED_TEXT,
    TWILIO_INBOUND_PATH,
    UNLINKED_TEXT,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    extract_pair_code,
    post_inbound,
    wait_record_key_for_value,
    wait_send_to,
)

# FakeTwilio + FakeWhatsApp are the 'twilio'/'whatsapp' mock leg; no LLM turn runs (a tool
# target dispatches directly, the pairing turns reach no target), so the 'llm' seam is unused.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp"),
    reason="FakeTwilio + FakeWhatsApp are the 'twilio'/'whatsapp' mock leg; real on the creds host",
)

_TOOL = "e2e_record"
# The person the turn ran as keys the probe list; the message is its value, so a per-fire
# marker resolves the key (person id) the turn minted.
_PAYLOAD_EXPR = "{key: .person_id, value: .message}"


def _transcript_path(route_name: str, thread_id: str) -> str:
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"


async def test_tool_state_keys_on_person_id_across_a_link_and_unlink(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port = bridge.stack.port_b

    # One tool target, multichannel ON, silent (null reply), on a twilio route and a whatsapp
    # route — one target, one handshake, one person namespace across both channels.
    await bridge.set_target_config(target_kind="tool", target_name=_TOOL, multichannel=True)
    exec_tw = uniq("l21-exec-tw")
    exec_wa = uniq("l21-exec-wa")
    await bridge.mint_key(user_id=exec_tw, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_wa, scopes=["e2e-all"])
    route_tw = uniq("l21-route-tw").replace("_", "-")
    route_wa = uniq("l21-route-wa").replace("_", "-")
    await bridge.create_tool_channel_route(
        route_name=route_tw,
        tool=_TOOL,
        execution_key=exec_tw,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        payload_expr=_PAYLOAD_EXPR,
        reply_expr="null",
    )
    await bridge.create_tool_channel_route(
        route_name=route_wa,
        tool=_TOOL,
        execution_key=exec_wa,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
        payload_expr=_PAYLOAD_EXPR,
        reply_expr="null",
    )

    async def twilio_msg(text: str) -> None:
        inbound = bridge.twilio_inbound(
            our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text=text, port=port
        )
        assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204

    async def whatsapp_msg(text: str) -> None:
        inbound = bridge.whatsapp_inbound(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=text
        )
        assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound, port=port)).status_code == 200

    # (a) Before pairing: the first message on each channel is a fresh provisional person, so
    # the two turns write under TWO DIFFERENT keys — both non-empty ids.
    marker_tw1 = uniq("l21-tw1")
    await twilio_msg(marker_tw1)
    id_tw = await wait_record_key_for_value(bridge, marker_tw1)
    marker_wa1 = uniq("l21-wa1")
    await whatsapp_msg(marker_wa1)
    id_wa = await wait_record_key_for_value(bridge, marker_wa1)
    assert id_tw, "twilio's first turn recorded no person id"
    assert id_wa, "whatsapp's first turn recorded no person id"
    assert id_tw != id_wa, f"the two unlinked channels shared a person id ({id_tw!r})"

    # (b) /link on twilio mints a code, redeemed from whatsapp into a merge → linked.
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
    (linked_reply,) = await wait_send_to(bridge.fake_whatsapp, to=BRIDGE_WHATSAPP_CLIENT, needle=LINKED_TEXT)
    assert linked_reply["body"] == LINKED_TEXT

    # (c) After pairing: a message from EACH channel writes under the SAME key — the merge
    # survivor, which is one of the two pre-link ids. The second channel now sees the first
    # channel's state (they share one key).
    marker_tw2 = uniq("l21-tw2")
    await twilio_msg(marker_tw2)
    key_tw2 = await wait_record_key_for_value(bridge, marker_tw2)
    marker_wa2 = uniq("l21-wa2")
    await whatsapp_msg(marker_wa2)
    key_wa2 = await wait_record_key_for_value(bridge, marker_wa2)
    assert key_tw2 == key_wa2, f"the linked channels wrote under different keys ({key_tw2!r} vs {key_wa2!r})"
    survivor = key_tw2
    assert survivor in {id_tw, id_wa}, f"the survivor {survivor!r} is neither pre-link id {id_tw!r}/{id_wa!r}"

    # (d) The post-link turns key the aggregated person thread ``bridge:@person:{survivor}``.
    # Read it back through the admin thread doors: the route lists that thread and its
    # transcript carries the post-link twilio turn.
    listing = await bridge.api().get(f"/api/conversations/{route_tw}/threads")
    person_threads = [item["thread_id"] for item in listing["items"] if item["thread_id"].endswith(survivor)]
    assert person_threads == [f"bridge:@person:{survivor}"], listing["items"]
    transcript = await bridge.api().get(_transcript_path(route_tw, f"bridge:@person:{survivor}"))
    assert marker_tw2 in [item["inbound_text"] for item in transcript["items"]], transcript["items"]

    # (e) /unlink from whatsapp detaches it: its next message writes under a NEW key, while
    # twilio still writes under the survivor.
    unlink_inbound = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text="/unlink"
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, unlink_inbound, port=port)).status_code == 200
    (unlinked_reply,) = await wait_send_to(bridge.fake_whatsapp, to=BRIDGE_WHATSAPP_CLIENT, needle=UNLINKED_TEXT)
    assert unlinked_reply["body"] == UNLINKED_TEXT

    marker_wa3 = uniq("l21-wa3")
    await whatsapp_msg(marker_wa3)
    key_wa3 = await wait_record_key_for_value(bridge, marker_wa3)
    assert key_wa3 != survivor, f"the unlinked whatsapp channel still wrote under the survivor ({survivor!r})"
    marker_tw3 = uniq("l21-tw3")
    await twilio_msg(marker_tw3)
    key_tw3 = await wait_record_key_for_value(bridge, marker_tw3)
    assert key_tw3 == survivor, f"twilio stopped writing under the survivor ({survivor!r} vs {key_tw3!r})"
