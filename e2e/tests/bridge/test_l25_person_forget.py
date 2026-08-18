"""L25 — erasing a linked person over a real bridge conversation.

A ``/link`` handshake folds a twilio address and a whatsapp address into ONE person, whose
post-link turns key the aggregated ``bridge:@person:{id}`` thread. The operator then erases
that person through ``DELETE /api/conversations/persons/{person_id}``, and the proof it worked
is threefold:

1. the aggregated thread is gone — its transcript reads the uniform 404 and the route listing
   no longer carries it (records + indexes erased);
2. the person row + its address→person index mappings are gone — the SAME twilio address's
   next message mints a FRESH provisional person id, so nothing remembers it was ever linked;
3. a second erase of the same id is idempotent (``erased: false``), never a 404.

Linking mechanics mirror L21; the erase mirrors the thread-forget door of L23.
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
    wait_record_key_for_value,
    wait_send_to,
)

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp"),
    reason="FakeTwilio + FakeWhatsApp are the 'twilio'/'whatsapp' mock leg; real on the creds host",
)

_TOOL = "e2e_record"
# The person the turn ran as keys the probe list; the message is its value, so a per-fire
# marker resolves the person id the turn minted.
_PAYLOAD_EXPR = "{key: .person_id, value: .message}"


def _transcript_path(route_name: str, thread_id: str) -> str:
    from urllib.parse import urlencode

    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"


async def test_erasing_a_linked_person_forgets_every_person_scoped_store(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port = bridge.stack.port_b

    await bridge.set_target_config(target_kind="tool", target_name=_TOOL, multichannel=True)
    exec_tw = uniq("l25-exec-tw")
    exec_wa = uniq("l25-exec-wa")
    await bridge.mint_key(user_id=exec_tw, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_wa, scopes=["e2e-all"])
    route_tw = uniq("l25-route-tw").replace("_", "-")
    route_wa = uniq("l25-route-wa").replace("_", "-")
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

    # Two provisional persons pre-link, then a /link + redeem merges them into one.
    await twilio_msg(uniq("l25-tw1"))
    await whatsapp_msg(uniq("l25-wa1"))
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

    # A post-link twilio turn writes under the survivor id and keys its aggregated thread.
    marker = uniq("l25-linked")
    await twilio_msg(marker)
    survivor = await wait_record_key_for_value(bridge, marker)
    person_thread = f"bridge:@person:{survivor}"
    transcript = await bridge.api().get(_transcript_path(route_tw, person_thread))
    assert marker in [item["inbound_text"] for item in transcript["items"]], transcript["items"]

    # Erase the person: the aggregated thread across both routes, the person row, its index
    # mappings. The removed count spans the routes the person wrote under.
    erased = await bridge.api().delete(f"/api/conversations/persons/{survivor}")
    assert erased["person_id"] == survivor
    assert erased["erased"] is True
    assert erased["removed"] >= 1

    # (1) the aggregated thread is gone: the transcript is the uniform 404 and the listing no
    # longer carries it.
    await bridge.api().get(_transcript_path(route_tw, person_thread), expect=404)
    listing = await bridge.api().get(f"/api/conversations/{route_tw}/threads")
    assert person_thread not in [item["thread_id"] for item in listing["items"]]

    # (2) the person row + index mappings are gone: the same twilio address now mints a FRESH
    # provisional person, so nothing remembers the erased link.
    reborn_marker = uniq("l25-reborn")
    await twilio_msg(reborn_marker)
    reborn = await wait_record_key_for_value(bridge, reborn_marker)
    assert reborn != survivor, f"the erased person's id survived into a new turn ({survivor!r})"

    # (3) a second erase of the same id is idempotent — not a 404.
    again = await bridge.api().delete(f"/api/conversations/persons/{survivor}")
    assert again == {"person_id": survivor, "removed": 0, "erased": False}
