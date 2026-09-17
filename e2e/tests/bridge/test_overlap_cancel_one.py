"""Overlap door: ``running=cancel`` + ``deliver=one`` supersedes the running and middle messages.

A held turn on message 1 is cancelled in favour of a newer message; message 2, itself overtaken
before its turn runs, is superseded too; one turn on message 3 answers. Each superseded record
carries no answer and names a strictly-later successor turn, and only message 3's reply is ever
sent. This proves the CANCEL mechanism and its watcher on the channel door.
"""

from __future__ import annotations

from collections.abc import Callable

from ._bridge_support import BridgeHarness, wait_probe_record, wait_record_status
from ._overlap_support import (
    create_web_tool_route,
    open_visitor,
    probe_payload_expr,
    reply_matching,
    send_web,
)

_HOLD_SECONDS = 3.0


async def test_cancel_one_supersedes_the_running_and_middle_messages(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("ov-cancel-one")
    route_name, identity = await create_web_tool_route(
        bridge,
        uniq,
        "ov-cancel-one",
        tool="e2e_overlap_probe",
        payload_expr=probe_payload_expr(marker, hold_seconds=_HOLD_SECONDS),
        overlap={"running": "cancel", "deliver": "one"},
    )
    web = await open_visitor(bridge, identity)

    t1, t2, t3 = uniq("ov-m1"), uniq("ov-m2"), uniq("ov-m3")

    # 1. Message 1's turn starts and holds (the probe entry is the barrier).
    id1 = await send_web(web, t1)
    await wait_probe_record(bridge, marker)

    # 2. Messages 2 and 3 are accepted while turn 1 holds: each accept refreshes the shared cancel
    #    marker, so the watcher cancels turn 1 in favour of the newest.
    id2 = await send_web(web, t2)
    id3 = await send_web(web, t3)

    # 3. Message 3's turn survives and answers.
    await web.frames(until=reply_matching(t3), deadline=40.0)
    r3 = await bridge.get_record(route_name, id3)
    assert r3["answer_status"] == "answered"
    assert r3["answer"] == t3
    assert r3["successor_id"] is None

    # 4. Messages 1 and 2 were both superseded — no answer — each naming a strictly-later successor
    #    turn. Message 2 was overtaken before its own turn ran, so its successor is message 3.
    r1 = await wait_record_status(bridge, route_name, id1, {"superseded"})
    assert r1["answer_status"] is None
    assert r1["answer"] is None
    assert r1["successor_id"] in (id2, id3)
    r2 = await wait_record_status(bridge, route_name, id2, {"superseded"})
    assert r2["answer_status"] is None
    assert r2["successor_id"] == id3
