"""Overlap door: an owed first-contact greeting rides the SUCCESSOR turn when the first-contact
turn is superseded — it is never dropped, and the superseded record carries no greeting.

A first-contact greeting is minted the moment a thread's person row is created (``_mint_and_owe_greeting``,
``conversations/turn/target.py``) and parked as OWED; the first turn on the thread that DELIVERS a reply
consumes it and prepends it as its leading message (``_deliver_with_owed_greeting``). When the
first-contact turn is instead superseded — cancelled by a newer message under ``running=cancel`` or
yielded by the turn body under a cooperative yield — it delivers nothing, so the greeting stays owed and
the first successor turn that delivers prepends it, exactly once.

The greeting is due on every door whose accept builds a multichannel context and creates the person row:
the CHANNEL door (``turn/intake.py`` ``accept``, ``door="channel"`` — twilio/whatsapp/web), the API door
(``turn/api_door.py``, ``door="api"``), and the interactions inbound-answer BRIDGE arm (which reaches the
same ``accept`` seam). The EVENT door is excluded: it runs no person-WRITE path and mints no greeting
(``turn/target.py`` — "no provisional mint, no greeting"). These legs cover cancel and yield on the
channel door and cancel on the API and bridge doors; the greeting template is a fixed string so the
delivered greeting message is exact.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    wait_probe_record,
    wait_record_status,
    wait_send_to,
)
from ._overlap_support import (
    create_web_tool_route,
    joined,
    open_visitor,
    probe_payload_expr,
    reply_matching,
    send_web,
    yield_payload_expr,
)

_PROBE = "e2e_overlap_probe"
_YIELD = "e2e_overlap_yield"
# The channel door holds as the channel cancel leg does; the API and bridge doors take the
# wider window their own overlap legs use, so the newer message always lands inside the hold.
_CHANNEL_HOLD_SECONDS = 3.0
_DOOR_HOLD_SECONDS = 4.0
_WAIT_SECONDS = 5.0
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _out_texts(frames: list[tuple[str, dict]]) -> list[str]:
    """The ordered texts of the outbound reply frames in a collected web stream."""
    return [data["text"] for event, data in frames if event == "chat.message" and data.get("direction") == "out"]


async def test_greeting_rides_the_successor_when_a_first_contact_turn_is_cancelled_on_the_channel_door(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Channel door (web), cancel path: the first-contact turn holds and is cancelled by a newer message;
    the greeting rides the surviving successor turn as its leading message, once, and the superseded
    records carry no greeting."""
    marker = uniq("ov-greet-cancel")
    greet = uniq("ov-greet-cancel-hello")
    await bridge.set_target_config(target_kind="tool", target_name=_PROBE, multichannel=True, greeting_template=greet)
    route_name, identity = await create_web_tool_route(
        bridge,
        uniq,
        "ov-greet-cancel",
        tool=_PROBE,
        payload_expr=probe_payload_expr(marker, hold_seconds=_CHANNEL_HOLD_SECONDS),
        overlap={"running": "cancel", "deliver": "one"},
    )
    web = await open_visitor(bridge, identity)

    t1, t2, t3 = uniq("ov-m1"), uniq("ov-m2"), uniq("ov-m3")

    # 1. Message 1 is the first contact: its person row is created, its greeting minted and owed, and
    #    its turn holds at the probe barrier.
    id1 = await send_web(web, t1)
    await wait_probe_record(bridge, marker)

    # 2. Two newer messages cancel the held first-contact turn; message 3's turn survives and answers.
    id2 = await send_web(web, t2)
    id3 = await send_web(web, t3)

    # 3. The successor delivers the owed greeting as its leading message, then its own answer — exactly
    #    those two outbound frames, in that order.
    frames = await web.frames(until=reply_matching(t3), deadline=40.0)
    assert _out_texts(frames) == [greet, t3], _out_texts(frames)

    # 4. The successor record carries both joined (greeting, blank line, answer) as its whole answer.
    r3 = await bridge.get_record(route_name, id3)
    assert r3["answer_status"] == "answered"
    assert r3["answer"] == f"{greet}\n\n{t3}", r3["answer"]
    assert r3["successor_id"] is None

    # 5. The superseded first-contact record carries no answer and no greeting; message 2 was superseded too.
    r1 = await wait_record_status(bridge, route_name, id1, {"superseded"})
    assert r1["answer_status"] is None
    assert r1["answer"] is None
    assert r1["successor_id"] in (id2, id3)
    r2 = await wait_record_status(bridge, route_name, id2, {"superseded"})
    assert r2["answer_status"] is None
    assert r2["answer"] is None


async def test_greeting_rides_the_successor_when_a_first_contact_turn_yields_on_the_channel_door(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Channel door (web), yield path: the first-contact turn yields to a newer message via the pending
    seam; the greeting rides the successor turn (which carries the yielded message under ``superseded``)
    as its leading message, once, and the yielded record carries no greeting."""
    marker = uniq("ov-greet-yield")
    greet = uniq("ov-greet-yield-hello")
    await bridge.set_target_config(target_kind="tool", target_name=_YIELD, multichannel=True, greeting_template=greet)
    route_name, identity = await create_web_tool_route(
        bridge,
        uniq,
        "ov-greet-yield",
        tool=_YIELD,
        payload_expr=yield_payload_expr(marker, wait_seconds=_WAIT_SECONDS),
        overlap={"running": "continue", "deliver": "all"},
    )
    web = await open_visitor(bridge, identity)

    t1, t2 = uniq("ov-m1"), uniq("ov-m2")

    # 1. Message 1 is the first contact: its greeting is minted and owed. Its turn gathers, records its
    #    ``entered`` barrier, and begins watching the pending seam.
    id1 = await send_web(web, t1)
    await wait_probe_record(bridge, f"{marker}:entered")

    # 2. A newer message lands pending; message 1 reads it and yields its turn to it.
    id2 = await send_web(web, t2)

    # 3. The successor (message 2, carrying the yielded message 1 under ``superseded``) delivers the owed
    #    greeting as its leading message, then the whole-turn answer.
    frames = await web.frames(until=reply_matching(t2), deadline=40.0)
    assert _out_texts(frames) == [greet, joined(t1, t2)], _out_texts(frames)

    r2 = await bridge.get_record(route_name, id2)
    assert r2["answer_status"] == "answered"
    assert r2["answer"] == f"{greet}\n\n{joined(t1, t2)}", r2["answer"]

    # 4. The yielded first-contact record carries no answer and no greeting.
    r1 = await wait_record_status(bridge, route_name, id1, {"superseded"})
    assert r1["answer_status"] is None
    assert r1["answer"] is None
    assert r1["successor_id"] == id2


async def test_greeting_rides_the_successor_on_the_api_door_when_the_first_contact_turn_is_cancelled(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """API door, cancel path: the first-contact sync-wait holds and is cancelled by a newer message on the
    same caller; the owed greeting rides the successor's answer, and the superseded marker carries none."""
    marker = uniq("ov-greet-api")
    greet = uniq("ov-greet-api-hello")
    await bridge.set_target_config(target_kind="tool", target_name=_PROBE, multichannel=True, greeting_template=greet)
    route_name = uniq("ov-greet-api-route").replace("_", "-")
    exec_key = uniq("ov-greet-api-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_api_route(
        route_name=route_name,
        tool=_PROBE,
        execution_key=exec_key,
        callback_url=_UNREACHABLE_CALLBACK,
        payload_expr=probe_payload_expr(marker, hold_seconds=_DOOR_HOLD_SECONDS),
        overlap={"running": "cancel", "deliver": "one"},
    )
    caller = await bridge.mint_key(user_id=uniq("ov-greet-api-caller"), scopes=["e2e-all"])
    end_user = uniq("ov-greet-api-user")

    t1, t2 = uniq("ov-in1"), uniq("ov-in2")

    # 1. Message 1 is the first contact for this caller: its person row is created and its greeting owed.
    #    Its sync-wait blocks server-side while its tool turn holds the turn open.
    first = asyncio.create_task(
        bridge.api(token=caller).post(
            f"/api/conversations/{route_name}/messages",
            json={"external_user_id": end_user, "text": t1, "wait_seconds": 20},
            expect=200,
        )
    )

    async def _turn_holds() -> bool:
        return bool(bridge.stack.records(marker))

    await wait_for_async(_turn_holds, deadline=15.0, message="message 1's tool turn never entered")

    # 2. Message 2 on the same thread cancels message 1 in its favour.
    second = asyncio.create_task(
        bridge.api(token=caller).post(
            f"/api/conversations/{route_name}/messages",
            json={"external_user_id": end_user, "text": t2, "wait_seconds": 20},
            expect=200,
        )
    )
    data1 = await asyncio.wait_for(first, timeout=30.0)
    data2 = await asyncio.wait_for(second, timeout=30.0)

    # 3. The superseded marker carries no answer and no greeting.
    assert data1["answer"]["status"] == "superseded"
    assert data1["answer"].get("answer") is None
    assert data1["answer"]["successor_id"] == data2["message_id"]

    # 4. The successor's answer carries the owed greeting as its leading message.
    assert data2["answer"]["status"] == "answered"
    assert data2["answer"]["answer"] == f"{greet}\n\n{t2}", data2["answer"]

    # 5. The poll door (record read) reads the same superseded marker with no greeting.
    record = await wait_record_status(bridge, route_name, data1["message_id"], {"delivered"})
    assert record["answer_status"] == "superseded"
    assert record["answer"] is None
    assert record["successor_id"] == data2["message_id"]


@pytest.mark.skipif(
    HarnessSettings().is_real("twilio"),
    reason="FakeTwilio is the 'twilio' mock leg; the real leg runs on the creds host",
)
async def test_greeting_rides_the_successor_on_the_bridge_door_when_the_first_contact_turn_is_cancelled(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Bridge door (twilio): an uncorrelated inbound is bridged to a fresh first-contact turn through the
    interactions inbound-answer ladder's bridge arm; a newer bridged message cancels it, and the owed
    greeting rides the successor's send as its leading message, once."""
    marker = uniq("ov-greet-bridge")
    greet = uniq("ov-greet-bridge-hello")
    await bridge.set_target_config(target_kind="tool", target_name=_PROBE, multichannel=True, greeting_template=greet)
    route_name = uniq("ov-greet-bridge-route").replace("_", "-")
    exec_key = uniq("ov-greet-bridge-exec")
    identity = f"+1555{secrets.randbelow(10**7):07d}"
    client = BRIDGE_TWILIO_CLIENT
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool=_PROBE,
        execution_key=exec_key,
        channel="twilio",
        our_identity=identity,
        payload_expr=probe_payload_expr(marker, hold_seconds=_DOOR_HOLD_SECONDS),
        overlap={"running": "cancel", "deliver": "one"},
    )
    port = bridge.stack.port_b

    t1, t2 = uniq("ov-m1"), uniq("ov-m2")

    # 1. The first uncorrelated inbound is bridged to a fresh first-contact turn (greeting minted and owed);
    #    that turn holds at the probe barrier.
    inbound1 = bridge.twilio_inbound(our_identity=identity, client=client, text=t1, port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound1, port=port)).status_code == 204
    await wait_probe_record(bridge, marker)

    # 2. A newer bridged inbound cancels the held turn; its own turn survives and replies.
    inbound2 = bridge.twilio_inbound(our_identity=identity, client=client, text=t2, port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound2, port=port)).status_code == 204

    # 3. The successor sends the owed greeting as its leading message, then its own answer — exactly those
    #    two sends, in that order (the cancelled turn sent nothing).
    sends = await wait_send_to(bridge.fake_twilio, to=client, count=2, deadline=_DOOR_HOLD_SECONDS + 20.0)
    assert [send["body"] for send in sends] == [greet, t2], [send["body"] for send in sends]

    (thread,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
    thread_id = thread["thread_id"]
    transcript = await bridge.api().get(
        f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"
    )
    (r1,) = [item for item in transcript["items"] if item["inbound_text"] == t1]
    (r2,) = [item for item in transcript["items"] if item["inbound_text"] == t2]
    assert r1["delivery_status"] == "superseded"
    assert r1["answer_status"] is None
    assert r1["successor_id"] == r2["message_id"]
    assert r2["answer_status"] == "answered"
    assert r2["answer"] == f"{greet}\n\n{t2}", r2["answer"]
