"""Overlap door: the interactions inbound-answer bridge arm schedules a turn under a cancel route.

Every channel inbound reaches the conversation engine THROUGH the shared interactions
inbound-answer ladder: the twilio plugin's webhook always calls
``tai42_app.channels.handle_inbound_answer`` first, and an uncorrelated reply (no pending ask on
the pair) is handed to ``tai42_app.conversations.accept`` by the ladder's bridge arm
(``channels/inbound.py`` ``_bridge`` → ``accept``, reached on ``NO_CORRELATION``). So a channel
overlap burst is scheduled through that bridge arm and governed by the one overlap chokepoint: a
held bridged turn is superseded by a newer bridged message under ``running=cancel``.

The ladder's other bridge sub-arms (``BRIDGED`` on a gone ask's 404, ``BRIDGED_KEPT`` on the
``on_mismatch=bridge`` digression) reach the SAME ``_bridge`` → ``accept`` → ``_schedule_turn``
seam, so the overlap chokepoint governs them identically by construction. They are not driven as a
separate live scenario here: they need a parked CHANNEL-delivered ask, which the twilio plugin
delivers from its single configured ``CHANNEL_TWILIO_FROM`` number, so a route exercising them must
bind that one shared identity — mutually exclusive with the other twilio specs on this shared
stack — and hinges on a structured ask's format rejection; the seam they exercise is the one this
test already drives through its ``NO_CORRELATION`` arm.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import TWILIO_INBOUND_PATH, BridgeHarness, post_inbound, wait_probe_record, wait_send_to
from ._overlap_support import probe_start_expr

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio"),
    reason="FakeTwilio is the 'twilio' mock leg; the real leg runs on the creds host",
)

_HOLD_SECONDS = 4.0


async def _record_by_text(bridge: BridgeHarness, route_name: str, thread_id: str, text: str) -> dict:
    transcript = await bridge.api().get(
        f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"
    )
    (item,) = [entry for entry in transcript["items"] if entry["inbound_text"] == text]
    return item


async def test_the_bridge_arm_schedules_an_overlap_turn_under_a_cancel_route(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("ov-bridge")
    route_name = uniq("ov-bridge-route").replace("_", "-")
    exec_key = uniq("ov-bridge-exec")
    identity = f"+1555{secrets.randbelow(10**7):07d}"
    client = BRIDGE_TWILIO_CLIENT
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_overlap_probe",
        execution_key=exec_key,
        channel="twilio",
        our_identity=identity,
        start_expr=probe_start_expr(marker, hold_seconds=_HOLD_SECONDS),
        overlap={"running": "cancel", "deliver": "one"},
    )
    port = bridge.stack.port_b

    t1, t2 = uniq("ov-m1"), uniq("ov-m2")

    # 1. An uncorrelated inbound (no pending ask) is bridged to a fresh conversation turn by the
    #    ladder's bridge arm; that turn holds (its probe entry is the barrier).
    inbound1 = bridge.twilio_inbound(our_identity=identity, client=client, text=t1, port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound1, port=port)).status_code == 204
    await wait_probe_record(bridge, marker)

    # 2. A newer uncorrelated inbound is bridged too; its accept refreshes the cancel marker, so the
    #    held bridged turn is superseded and the newer bridged turn survives and replies.
    inbound2 = bridge.twilio_inbound(our_identity=identity, client=client, text=t2, port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound2, port=port)).status_code == 204
    await wait_send_to(bridge.fake_twilio, to=client, needle=t2, deadline=_HOLD_SECONDS + 20.0)

    (thread,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
    thread_id = thread["thread_id"]
    r1 = await _record_by_text(bridge, route_name, thread_id, t1)
    r2 = await _record_by_text(bridge, route_name, thread_id, t2)
    assert r1["delivery_status"] == "superseded"
    assert r1["answer_status"] is None
    assert r1["successor_id"] == r2["message_id"]
    assert r2["answer_status"] == "answered"
    assert r2["answer"] == t2
