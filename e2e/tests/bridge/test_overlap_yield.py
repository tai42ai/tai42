"""Overlap door: a tool target yields its turn to a newer message via the pending seam.

A tool target reads the pending seam (``tai42_app.conversations.pending_messages``) with the
ambient turn's own lead id and, when a newer message is waiting, hands its turn over by raising
``TurnSupersededError`` with the newest pending id as the successor. Under ``running=continue`` +
``deliver=all`` the yielded message is resolved ``superseded`` (no reply), and the next turn — the
newer message it yielded to — carries the yielded message under ``superseded``. This proves the
KNOW + YIELD mechanism end to end: the ambient turn ref, the pending seam, and the cooperative
supersede.
"""

from __future__ import annotations

from collections.abc import Callable

from ._bridge_support import BridgeHarness, wait_probe_entries, wait_probe_record, wait_record_status
from ._overlap_support import (
    create_web_tool_route,
    entry_texts,
    joined,
    open_visitor,
    reply_matching,
    send_web,
    yield_start_expr,
)

_WAIT_SECONDS = 5.0


async def test_a_tool_that_yields_hands_its_turn_to_the_newer_message(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("ov-yield")
    route_name, identity = await create_web_tool_route(
        bridge,
        uniq,
        "ov-yield",
        tool="e2e_overlap_yield",
        start_expr=yield_start_expr(marker, wait_seconds=_WAIT_SECONDS),
        overlap={"running": "continue", "deliver": "all"},
    )
    web = await open_visitor(bridge, identity)

    t1, t2 = uniq("ov-m1"), uniq("ov-m2")

    # 1. Message 1's turn starts and, after gathering, records its ``entered`` barrier and begins
    #    watching the pending seam.
    id1 = await send_web(web, t1)
    await wait_probe_record(bridge, f"{marker}:entered")

    # 2. A newer message is accepted AFTER turn 1 has gathered, so it is pending, not merged. Turn 1
    #    reads it off the pending seam and yields.
    id2 = await send_web(web, t2)

    # 3. Two turns record: turn 1 (which saw message 2 pending) then the surviving turn 2.
    entries = await wait_probe_entries(bridge, marker, 2, deadline=40.0)
    assert entries[0]["message"] == t1
    assert entries[0]["pending"] == [id2], "turn 1 read the newer message off the pending seam"
    assert entry_texts(entries[1], "superseded") == [t1], "the yielded message rides the next turn's superseded"
    assert entries[1]["message"] == joined(t1, t2)

    # 4. Only message 2's turn answered; message 1 was superseded in its favour, no reply.
    await web.frames(until=reply_matching(t2), deadline=40.0)
    r1 = await wait_record_status(bridge, route_name, id1, {"superseded"})
    assert r1["answer_status"] is None
    assert r1["successor_id"] == id2
    r2 = await bridge.get_record(route_name, id2)
    assert r2["answer_status"] == "answered"
    assert r2["answer"] == joined(t1, t2)
