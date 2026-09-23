"""Overlap door: an event turn on a cancel route is excluded — never batched, superseded, or a canceller.

The overlap policy governs participant MESSAGE turns only. An event submitted on a ``running=cancel``
+ ``deliver=all`` route runs as its own turn: it carries no ``messages`` batch, it arms no cancel
watcher (so a newer participant message accepted while it is in flight never supersedes it), and it
sets no cancel marker (so it cancels nothing). This proves the event door is excluded by kind at
the one overlap chokepoint.
"""

from __future__ import annotations

from collections.abc import Callable

from ._bridge_support import BridgeHarness, wait_probe_entries
from ._overlap_support import create_web_tool_route, open_visitor, reply_matching, send_web

_HOLD_SECONDS = 3.0
_EVENT_KIND = "status.update"


async def test_an_event_turn_on_a_cancel_route_is_never_batched_superseded_or_a_canceller(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("ov-event")
    # The event turn nulls ``message``, so the probe coalesces the event kind in as its reply text
    # (a null reply would end the event turn silently and blur "survived" from "was superseded").
    start_expr = (
        f'{{key: "{marker}", message: (.message // .event.kind), messages: .messages, '
        f"superseded: .superseded, hold_seconds: {_HOLD_SECONDS}}}"
    )
    route_name, identity = await create_web_tool_route(
        bridge,
        uniq,
        "ov-event",
        tool="e2e_overlap_probe",
        start_expr=start_expr,
        overlap={"running": "cancel", "deliver": "all"},
    )
    web = await open_visitor(bridge, identity)

    t1, t2 = uniq("ov-m1"), uniq("ov-m2")

    # 1. Compose the thread with an ordinary message turn (an event never mints a thread).
    await send_web(web, t1)
    await web.frames(until=reply_matching(t1), deadline=40.0)

    # 2. Submit an event on that thread; its turn starts and holds.
    accepted = await bridge.api().post(
        f"/api/conversations/{route_name}/events",
        json={"address": web.visitor_id, "event": {"event_id": uniq("evt"), "kind": _EVENT_KIND, "payload": {}}},
        expect=202,
    )
    event_id = accepted["message_id"]

    # 3. The event turn recorded: it carries NO ``messages`` batch — excluded from batching by kind.
    entries = await wait_probe_entries(bridge, marker, 2, deadline=40.0)
    event_entry = entries[1]
    assert event_entry["message"] == _EVENT_KIND
    assert event_entry["messages"] is None, "an event turn is never batched, so it carries no messages key"

    # 4. A newer participant message is accepted while the event turn is still in flight. An event
    #    turn arms no cancel watcher, so this never supersedes it.
    id2 = await send_web(web, t2)

    # 5. The event turn survived to its own answer (not superseded), and the newer message ran its
    #    own turn (the event set no cancel marker, so it cancelled nothing).
    await web.frames(until=reply_matching(t2), deadline=40.0)
    event_record = await bridge.get_record(route_name, event_id)
    assert event_record["inbound_kind"] == "event"
    assert event_record["delivery_status"] not in ("merged", "superseded")
    assert event_record["answer_status"] == "answered"
    assert event_record["successor_id"] is None

    r2 = await bridge.get_record(route_name, id2)
    assert r2["answer_status"] == "answered"
    assert r2["answer"] == t2
    assert r2["successor_id"] is None
