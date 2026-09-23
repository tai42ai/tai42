"""Overlap door: ``running=cancel`` + ``deliver=all`` carries the cancelled message in ``superseded``.

A held turn on message 1 is cancelled; then ONE turn carries messages 2 (its lead) and 3 (merged),
and the cancelled message 1 rides that turn's payload under ``superseded`` — its text is not lost.
The surviving turn's whole text is the superseded text then the batch texts, in acceptance order.
This proves CANCEL and CARRY together: a cancelled turn's message still reaches the next turn.
"""

from __future__ import annotations

from collections.abc import Callable

from ._bridge_support import BridgeHarness, wait_probe_entries, wait_probe_record, wait_record_status
from ._overlap_support import (
    create_web_tool_route,
    entry_texts,
    joined,
    open_visitor,
    probe_start_expr,
    reply_matching,
    send_web,
)

_HOLD_SECONDS = 3.0


async def test_cancel_all_carries_the_cancelled_message_in_superseded(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("ov-cancel-all")
    route_name, identity = await create_web_tool_route(
        bridge,
        uniq,
        "ov-cancel-all",
        tool="e2e_overlap_probe",
        start_expr=probe_start_expr(marker, hold_seconds=_HOLD_SECONDS),
        overlap={"running": "cancel", "deliver": "all"},
    )
    web = await open_visitor(bridge, identity)

    t1, t2, t3 = uniq("ov-m1"), uniq("ov-m2"), uniq("ov-m3")

    # 1. Message 1's turn starts and holds (the probe entry is the barrier).
    id1 = await send_web(web, t1)
    await wait_probe_record(bridge, marker)

    # 2. Messages 2 and 3 are accepted while turn 1 holds. The newer marker cancels turn 1; then
    #    message 2's turn carries 2 and 3 and, because message 1's successor rides this batch, it
    #    carries message 1 under ``superseded``.
    id2 = await send_web(web, t2)
    id3 = await send_web(web, t3)

    # 3. Exactly two turns record: turn 1 (before its cancel) and the surviving batch turn 2.
    entries = await wait_probe_entries(bridge, marker, 2, deadline=40.0)
    assert entries[0]["message"] == t1
    assert entry_texts(entries[1], "messages") == [t2, t3], "the surviving turn carries 2 (lead) and 3 (merged)"
    assert entry_texts(entries[1], "superseded") == [t1], "the cancelled message 1 rides the batch's superseded"
    assert entries[1]["message"] == joined(t1, t2, t3), "the whole turn is the superseded text then the batch texts"

    # 4. The surviving turn's whole-text reply reaches the visitor.
    await web.frames(until=reply_matching(t2), deadline=40.0)

    # 5. The records: 1 superseded (into a batch member), 3 merged into 2, 2 answered the whole text.
    r1 = await wait_record_status(bridge, route_name, id1, {"superseded"})
    assert r1["answer_status"] is None
    assert r1["successor_id"] in (id2, id3)
    r2 = await bridge.get_record(route_name, id2)
    assert r2["answer_status"] == "answered"
    assert r2["answer"] == joined(t1, t2, t3)
    r3 = await bridge.get_record(route_name, id3)
    assert r3["delivery_status"] == "merged"
    assert r3["successor_id"] == id2
