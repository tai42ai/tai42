"""L23 — forgetting one conversation thread over a real bridge conversation.

Two turns on one routed twilio pair leave a thread whose agent checkpoint holds the first
turn's fact — proven the way L16 proves continuity: the SECOND turn's request to the model
replays the first turn's history. The operator then forgets the thread through
``DELETE /api/conversations/{route}/thread?thread_id=``, and the proof it worked is
twofold: the transcript reads back the uniform 404 and the listing empties (the records and
indexes are gone), and a THIRD turn on the same address no longer carries the fact (the
agent checkpoint is gone, so memory restarts from empty).

The thread id rides the delete door's ``?thread_id=`` query, the same as the transcript read
door, so it round-trips whatever it holds.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from urllib.parse import urlencode

import pytest
from _bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    request_mentions,
    script_reply,
    wait_twilio_send,
)

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings

# The scripted LLM turns are the 'llm' mock leg; the twilio signed inbound the 'twilio' one.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm") or HarnessSettings().is_real("twilio"),
    reason="scripted-LLM + twilio stub are the 'llm'/'twilio' mock leg; real on the creds host",
)

_AGENT = "tools_agent"


def _transcript_path(route_name: str, thread_id: str) -> str:
    """The transcript read door's URL — the thread id is a query value there."""
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"


def _thread_delete_path(route_name: str, thread_id: str) -> str:
    """The delete door's URL — the thread id is a query value there, the same as the read door."""
    return f"/api/conversations/{route_name}/thread?{urlencode({'thread_id': thread_id})}"


async def test_deleting_a_thread_forgets_its_memory_and_its_transcript(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port = bridge.stack.port_b
    route_name = uniq("l23-route").replace("_", "-")
    execution_key = uniq("l23-exec")
    identity = f"+1555{secrets.randbelow(10**7):07d}"
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name, agent=_AGENT, execution_key=execution_key, channel="twilio", our_identity=identity
    )

    fact = uniq("l23-fact")
    ans1 = uniq("l23-a1")
    ans2 = uniq("l23-a2")
    ans3 = uniq("l23-a3")
    # Three AGENT turns, one scripted answer each, in order: state the fact, ask (holds it),
    # ask again after the delete (no longer holds it).
    script_reply(bridge.llm_stub, ans1, ans2, ans3)

    async def twilio_turn(text: str, answer: str) -> None:
        inbound = bridge.twilio_inbound(our_identity=identity, client=BRIDGE_TWILIO_CLIENT, text=text, port=port)
        assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204
        await wait_twilio_send(bridge.fake_twilio, answer)

    # Turn 0 states the fact; turn 1 (LLM request 1) runs against the checkpoint that holds it.
    await twilio_turn(f"my secret is {fact}", ans1)
    await twilio_turn("what is my secret?", ans2)
    assert request_mentions(bridge.llm_stub, 1, fact), (
        "the agent did not hold the fact across turns before the delete; there is no memory to forget"
    )

    # The thread is real: it lists, and its transcript reads both exchanges back.
    (listed,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
    thread_id = listed["thread_id"]
    assert listed["client_address"] == BRIDGE_TWILIO_CLIENT
    transcript = await bridge.api().get(_transcript_path(route_name, thread_id))
    assert transcript["total"] == 2

    # Forget it — the memory delete door.
    removed = await bridge.api().delete(_thread_delete_path(route_name, thread_id))
    assert removed["thread_id"] == thread_id
    assert removed["removed"] == 2

    # The records and indexes are gone: the transcript is the uniform 404 and the listing empties.
    await bridge.api().get(_transcript_path(route_name, thread_id), expect=404)
    assert (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["total"] == 0

    # Turn 2 (LLM request 2) on the SAME address no longer carries the fact: the agent
    # checkpoint was forgotten, so its memory restarts from empty.
    await twilio_turn("what is my secret?", ans3)
    assert not request_mentions(bridge.llm_stub, 2, fact), (
        "the deleted thread's memory survived into a new turn; the checkpoint was not forgotten"
    )
