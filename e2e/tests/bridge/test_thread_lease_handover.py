"""The cross-worker thread lease hands a busy thread to the next turn on its release.

Two messages on one thread are sent back to back to different workers. Worker A's turn holds the
thread's lease (its tool target holds the turn open); worker B accepts the second message, finds
the thread busy, and waits for A's release announcement. B's turn starts only after A's turn
ends, and both messages are answered once, in order.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

import pytest

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import WaitTimeoutError, wait_for_async

from ._bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    wait_probe_entries,
    wait_probe_record,
    wait_send_to,
)
from ._overlap_support import probe_start_expr

# FakeTwilio's signed inbound is the 'twilio' mock leg; the tool target runs directly (no LLM).
pytestmark = [
    pytest.mark.needs(
        "kind:channels:twilio",
        "kind:identity",
        "probe-tools",
        "helper:twilio",
        "setting:conversations",
        "setting:seeded-access-control",
        "store:redis",
        "topology:replicas",
    ),
    pytest.mark.skipif(
        HarnessSettings().is_real("twilio"),
        reason="FakeTwilio is the 'twilio' mock leg; the real leg runs on the creds host",
    ),
]

# Each turn holds this long, so worker B's turn is provably waiting while worker A's runs.
_HOLD_SECONDS = 4.0


async def test_a_busy_thread_passes_to_the_waiting_worker_when_the_turn_ends(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port_a = bridge.stack.port_a
    port_b = bridge.stack.port_b
    marker = uniq("lease-handover")
    route_name = uniq("lease-handover-route").replace("_", "-")
    exec_key = uniq("lease-handover-exec")
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
    )

    t1, t2 = uniq("lease-m1"), uniq("lease-m2")

    # 1. Message 1's turn runs on worker A and holds the thread (its probe entry is the barrier).
    inbound_a = bridge.twilio_inbound(our_identity=identity, client=client, text=t1, port=port_a)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound_a, port=port_a)).status_code == 204
    await wait_probe_record(bridge, marker)

    # 2. Message 2 is accepted on worker B while A's turn still holds the thread.
    inbound_b = bridge.twilio_inbound(our_identity=identity, client=client, text=t2, port=port_b)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound_b, port=port_b)).status_code == 204

    # 3. For half of A's hold, B's turn does not start: it waits for the thread.
    async def _second_turn_started() -> bool:
        return len(bridge.stack.records(marker)) >= 2

    with pytest.raises(WaitTimeoutError):
        await wait_for_async(_second_turn_started, deadline=_HOLD_SECONDS / 2, message="B's turn waits for the thread")

    # 4. A's turn ends and releases the thread; B's turn then runs, in another process.
    entries = await wait_probe_entries(bridge, marker, 2, deadline=_HOLD_SECONDS * 2 + 20.0)
    assert [entry["message"] for entry in entries] == [t1, t2]
    assert entries[0]["pid"] != entries[1]["pid"], "the two turns ran on the same worker"

    # 5. Both messages are answered once, in order.
    await wait_send_to(bridge.fake_twilio, to=client, needle=t1, deadline=20.0)
    await wait_send_to(bridge.fake_twilio, to=client, needle=t2, deadline=_HOLD_SECONDS + 20.0)
    bodies = [record["body"] for record in bridge.fake_twilio.messages if record.get("to") == client]
    first = next(index for index, body in enumerate(bodies) if t1 in body)
    second = next(index for index, body in enumerate(bodies) if t2 in body)
    assert first < second
