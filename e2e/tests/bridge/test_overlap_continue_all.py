"""Overlap door: ``running=continue`` + ``deliver=all`` carries the burst after the running turn.

A held turn on message 1 runs alone; while it holds, messages 2 and 3 are accepted; then exactly
ONE later turn carries 2 (its lead) and 3 (merged into it) as its whole text, and its tool-target
payload names the batch under ``messages``. Message 2 answers; message 3 is ``merged`` with its
``successor_id`` naming message 2. This proves the CARRY mechanism on the channel door — the
web channel's ``accept`` schedules every web message through the one overlap chokepoint, so a
burst behind a running turn rides one turn.
"""

from __future__ import annotations

from collections.abc import Callable

from ._bridge_support import BridgeHarness, wait_probe_entries, wait_probe_record
from ._overlap_support import (
    create_web_tool_route,
    entry_texts,
    joined,
    open_visitor,
    probe_payload_expr,
    reply_matching,
    send_web,
)

# The web channel has no vendor, so it is always real — no mock-leg skip. The tool target runs
# directly under the route's execution key (no scripted LLM), so no 'llm' leg is involved either.

_HOLD_SECONDS = 3.0


async def test_continue_all_carries_the_burst_into_one_later_turn(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("ov-cont-all")
    route_name, identity = await create_web_tool_route(
        bridge,
        uniq,
        "ov-cont-all",
        tool="e2e_overlap_probe",
        payload_expr=probe_payload_expr(marker, hold_seconds=_HOLD_SECONDS),
        overlap={"running": "continue", "deliver": "all"},
    )
    web = await open_visitor(bridge, identity)

    t1, t2, t3 = uniq("ov-m1"), uniq("ov-m2"), uniq("ov-m3")

    # 1. Message 1's turn starts and holds (its probe entry is the turn-entered barrier).
    id1 = await send_web(web, t1)
    await wait_probe_record(bridge, marker)

    # 2. Messages 2 and 3 are accepted while turn 1 still holds, so they fall behind it in the
    #    FIFO and 3 is a follower of 2's turn.
    id2 = await send_web(web, t2)
    id3 = await send_web(web, t3)

    # 3. Exactly two turns record: turn 1 (the lead alone) then turn 2 carrying 2 and 3.
    entries = await wait_probe_entries(bridge, marker, 2, deadline=40.0)
    assert entries[0]["message"] == t1
    assert entry_texts(entries[0], "messages") == [t1], "a single-member batch still names its lone member"
    assert entries[1]["message"] == joined(t2, t3)
    assert entry_texts(entries[1], "messages") == [t2, t3], "the second turn carries 2 (lead) and 3 (merged)"

    # 4. Turn 2's whole-text reply reaches the visitor — the CARRY delivered as one turn.
    await web.frames(until=reply_matching(joined(t2, t3)), deadline=40.0)

    # 5. The records: 1 and 2 answered, 3 merged into 2.
    r1 = await bridge.get_record(route_name, id1)
    r2 = await bridge.get_record(route_name, id2)
    r3 = await bridge.get_record(route_name, id3)
    assert r1["answer_status"] == "answered"
    assert r1["answer"] == t1
    assert r2["answer_status"] == "answered"
    assert r2["answer"] == joined(t2, t3)
    assert r2["successor_id"] is None
    assert r3["delivery_status"] == "merged"
    assert r3["answer_status"] is None
    assert r3["successor_id"] == id2
