"""Overlap door: the cancel watcher reads a marker a SIBLING worker set — the cross-worker path.

The overlap cancel marker lives in the shared conversations Redis, and the watcher runs in the
worker holding the turn — there is no local fast path. A turn held on ``port_a`` is cancelled by a
newer message accepted on ``port_b``: worker B's accept refreshes the shared marker, worker A's
watcher reads it and supersedes the held turn. This proves CANCEL crosses the worker boundary.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

import pytest

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import TWILIO_INBOUND_PATH, BridgeHarness, post_inbound, wait_probe_record, wait_send_to
from ._overlap_support import probe_payload_expr

# FakeTwilio's signed inbound is the 'twilio' mock leg; the tool target runs directly (no LLM).
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio"),
    reason="FakeTwilio is the 'twilio' mock leg; the real leg runs on the creds host",
)

_HOLD_SECONDS = 4.0


async def _record_by_text(bridge: BridgeHarness, route_name: str, thread_id: str, text: str) -> dict:
    from urllib.parse import urlencode

    query = urlencode({"thread_id": thread_id})
    transcript = await bridge.api().get(f"/api/conversations/{route_name}/transcript?{query}")
    (item,) = [entry for entry in transcript["items"] if entry["inbound_text"] == text]
    return item


async def test_a_marker_a_sibling_worker_set_cancels_the_held_turn(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port_a = bridge.stack.port_a
    port_b = bridge.stack.port_b
    marker = uniq("ov-xworker")
    route_name = uniq("ov-xw-route").replace("_", "-")
    exec_key = uniq("ov-xw-exec")
    identity = f"+1555{secrets.randbelow(10**7):07d}"
    client = BRIDGE_TWILIO_CLIENT
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_overlap_probe",
        execution_key=exec_key,
        channel="twilio",
        our_identity=identity,
        payload_expr=probe_payload_expr(marker, hold_seconds=_HOLD_SECONDS),
        overlap={"running": "cancel", "deliver": "one"},
    )

    t1, t2 = uniq("ov-m1"), uniq("ov-m2")

    # 1. Message 1's turn is driven on worker A and holds (its probe entry is the barrier).
    inbound_a = bridge.twilio_inbound(our_identity=identity, client=client, text=t1, port=port_a)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound_a, port=port_a)).status_code == 204
    await wait_probe_record(bridge, marker)

    # 2. A newer message is accepted on worker B; its accept writes the shared cancel marker.
    inbound_b = bridge.twilio_inbound(our_identity=identity, client=client, text=t2, port=port_b)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound_b, port=port_b)).status_code == 204

    # 3. Worker A's watcher reads the sibling's marker and supersedes the held turn; worker B's
    #    turn survives and delivers its reply.
    await wait_send_to(bridge.fake_twilio, to=client, needle=t2, deadline=_HOLD_SECONDS + 20.0)

    listing = await bridge.api().get(f"/api/conversations/{route_name}/threads")
    (thread,) = listing["items"]
    thread_id = thread["thread_id"]
    r1 = await _record_by_text(bridge, route_name, thread_id, t1)
    r2 = await _record_by_text(bridge, route_name, thread_id, t2)
    assert r1["delivery_status"] == "superseded"
    assert r1["answer_status"] is None
    assert r1["successor_id"] == r2["message_id"]
    assert r2["answer_status"] == "answered"
    assert r2["answer"] == t2
