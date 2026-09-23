"""Overlap door: the API door delivers merged/superseded markers carrying the successor id.

Both overlap outcomes reach the API caller exactly as a ``silent`` marker does: an answerless
``ConversationAnswer`` carrying the ``status`` and the ``successor_id`` of the turn that took the
message's place. The sync-wait returns the superseded marker inline in its ``200``, and the poll
door (the record read) reads both the superseded and the merged markers with their successor. This
proves the API door surfaces overlap outcomes.

The signed HTTPS callback delivery of the same marker is proven at the unit level
(``test_the_api_door_posts_a_merged_marker_to_the_callback``): the contract forces an absolute
HTTPS ``callback_url`` with no insecure opt-out, so this harness has no TLS callback receiver to
stand in — here the unreachable callback drives the marker down the same durable delivery pipeline
until it lands ``failed``, proving the marker travelled it (and the poll read sees the marker).

The tool target is driven (not an agent) because the cancel is proven cleanly on it — the held
``e2e_overlap_probe`` runs inline on the event loop, so the cancel watcher cancels it and the turn
resolves ``superseded``. No scripted LLM is involved.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from tai42_e2e.waiting import wait_for_async

from ._bridge_support import BridgeHarness, wait_probe_record, wait_record_status
from ._overlap_support import probe_start_expr

_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"
_HOLD_SECONDS = 4.0


async def _api_tool_route(
    bridge: BridgeHarness, uniq: Callable[[str], str], tag: str, *, start_expr: str, overlap: dict[str, object]
) -> str:
    route_name = uniq(f"{tag}-route").replace("_", "-")
    exec_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_api_route(
        route_name=route_name,
        tool="e2e_overlap_probe",
        execution_key=exec_key,
        callback_url=_UNREACHABLE_CALLBACK,
        start_expr=start_expr,
        overlap=overlap,
    )
    return route_name


async def test_the_api_door_sync_wait_and_poll_see_a_superseded_marker(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    marker = uniq("ov-api-sup")
    route_name = await _api_tool_route(
        bridge,
        uniq,
        "ov-api-sup",
        start_expr=probe_start_expr(marker, hold_seconds=_HOLD_SECONDS),
        overlap={"running": "cancel", "deliver": "one"},
    )
    caller = await bridge.mint_key(user_id=uniq("ov-api-caller"), scopes=["e2e-all"])
    end_user = uniq("ov-api-user")

    # Message 1's sync-wait blocks server-side while its tool target holds the turn open.
    first = asyncio.create_task(
        bridge.api(token=caller).post(
            f"/api/conversations/{route_name}/messages",
            json={"external_user_id": end_user, "text": uniq("ov-in1"), "wait_seconds": 20},
            expect=200,
        )
    )

    async def _turn_holds() -> bool:
        return bool(bridge.stack.records(marker))

    await wait_for_async(_turn_holds, deadline=15.0, message="message 1's tool turn never entered")

    # Message 2 on the same thread cancels message 1 in its favour.
    second = asyncio.create_task(
        bridge.api(token=caller).post(
            f"/api/conversations/{route_name}/messages",
            json={"external_user_id": end_user, "text": uniq("ov-in2"), "wait_seconds": 20},
            expect=200,
        )
    )
    data1 = await asyncio.wait_for(first, timeout=30.0)
    data2 = await asyncio.wait_for(second, timeout=30.0)

    # The sync-wait carried the superseded marker inline, naming message 2 as the successor.
    assert data1["answer"]["status"] == "superseded"
    assert data1["answer"].get("answer") is None
    assert data1["answer"]["successor_id"] == data2["message_id"]
    assert data2["answer"]["status"] == "answered"

    # The poll door (the record read) reads the delivered superseded marker with its successor.
    record = await wait_record_status(bridge, route_name, data1["message_id"], {"delivered"})
    assert record["answer_status"] == "superseded"
    assert record["answer"] is None
    assert record["successor_id"] == data2["message_id"]


async def test_the_api_door_poll_sees_a_merged_marker(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    marker = uniq("ov-api-mrg")
    route_name = await _api_tool_route(
        bridge,
        uniq,
        "ov-api-mrg",
        start_expr=probe_start_expr(marker),
        overlap={"deliver": "all", "settle_seconds": 2},
    )
    caller = await bridge.mint_key(user_id=uniq("ov-api-caller2"), scopes=["e2e-all"])
    end_user = uniq("ov-api-user2")

    # Both async messages land inside the lead's settle window, so message 2 merges into message 1.
    lead = await bridge.api(token=caller).post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": end_user, "text": uniq("ov-in1")},
        expect=202,
    )
    follower = await bridge.api(token=caller).post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": end_user, "text": uniq("ov-in2")},
        expect=202,
    )
    await wait_probe_record(bridge, marker)

    # The merged marker rides the durable delivery pipeline to the unreachable callback → failed;
    # the poll read sees the marker and its successor either way.
    merged = await wait_record_status(bridge, route_name, follower["message_id"], {"failed"}, deadline=25.0)
    assert merged["answer_status"] == "merged"
    assert merged["answer"] is None
    assert merged["successor_id"] == lead["message_id"]

    lead_record = await wait_record_status(
        bridge, route_name, lead["message_id"], {"failed", "delivered"}, deadline=25.0
    )
    assert lead_record["answer_status"] == "answered"
