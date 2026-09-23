"""Overlap door: the settle window rides a burst with no turn running into one turn.

With ``deliver=all`` and a ``settle_seconds`` window and NO turn already running, a turn starts no
earlier than the window after its lead was accepted, so three messages dropped inside the window
all ride ONE turn: message 1 is the lead, 2 and 3 are merged into it, and the tool payload names
the whole batch under ``messages``. This proves the SETTLE window on the channel door.
"""

from __future__ import annotations

from collections.abc import Callable

from ._bridge_support import BridgeHarness, wait_probe_entries
from ._overlap_support import (
    create_web_tool_route,
    entry_texts,
    joined,
    open_visitor,
    probe_start_expr,
    reply_matching,
    send_web,
)

_SETTLE_SECONDS = 2


async def test_a_settle_window_rides_a_burst_into_one_turn(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    marker = uniq("ov-settle")
    route_name, identity = await create_web_tool_route(
        bridge,
        uniq,
        "ov-settle",
        tool="e2e_overlap_probe",
        start_expr=probe_start_expr(marker),
        overlap={"deliver": "all", "settle_seconds": _SETTLE_SECONDS},
    )
    web = await open_visitor(bridge, identity)

    t1, t2, t3 = uniq("ov-m1"), uniq("ov-m2"), uniq("ov-m3")

    # All three land inside message 1's settle window, so the one turn that starts after the window
    # gathers the whole burst.
    id1 = await send_web(web, t1)
    id2 = await send_web(web, t2)
    id3 = await send_web(web, t3)

    # Exactly one turn ran, carrying the whole burst in order as its ``messages`` and whole text.
    entries = await wait_probe_entries(bridge, marker, 1, deadline=40.0)
    assert entry_texts(entries[0], "messages") == [t1, t2, t3]
    assert entries[0]["message"] == joined(t1, t2, t3)

    await web.frames(until=reply_matching(joined(t1, t2, t3)), deadline=40.0)

    r1 = await bridge.get_record(route_name, id1)
    assert r1["answer_status"] == "answered"
    assert r1["answer"] == joined(t1, t2, t3)
    for follower_id in (id2, id3):
        follower = await bridge.get_record(route_name, follower_id)
        assert follower["delivery_status"] == "merged"
        assert follower["successor_id"] == id1
