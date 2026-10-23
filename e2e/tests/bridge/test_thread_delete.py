"""Forgetting one conversation thread over a real bridge conversation.

Two turns on one routed twilio pair leave a thread whose agent checkpoint holds the first
turn's fact — proven the way test_agent_thread_continuity proves continuity: the SECOND turn's request to the model
replays the first turn's history. The operator then forgets the thread through
``DELETE /api/conversations/{route}/thread?thread_id=``, and the proof it worked is
twofold: the transcript reads back the uniform 404 and the listing empties (the records and
indexes are gone), and a THIRD turn on the same address no longer carries the fact (the
agent checkpoint is gone, so memory restarts from empty).

Forgetting a thread forgets every message the platform sent into it too: a thread holding a
participant's message and its reply, a ``notify_user`` made during that turn, and an ask with the
participant's reply is forgotten through the same door, and afterwards no record, outbound id,
message-ref or ask key in the stack's Redis names any of those messages.

The thread id rides the delete door's ``?thread_id=`` query, the same as the transcript read
door, so it round-trips whatever it holds.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_FROM
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    cancel_and_join,
    post_inbound,
    redis_keys_naming,
    request_mentions,
    script_reply,
    wait_twilio_send,
)

# The scripted LLM turns are the 'llm' mock leg; the twilio signed inbound the 'twilio' one.
pytestmark = [
    pytest.mark.needs(
        "kind:channels:twilio",
        "kind:identity",
        "helper:llm",
        "helper:twilio",
        "setting:agent",
        "setting:checkpoint:memory",
        "setting:conversations",
        "setting:seeded-access-control",
    ),
    pytest.mark.skipif(
        HarnessSettings().is_real("llm") or HarnessSettings().is_real("twilio"),
        reason="scripted-LLM + twilio stub are the 'llm'/'twilio' mock leg; real on the creds host",
    ),
]

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


@pytest.mark.needs("probe-tools", "setting:router:notifications", "setting:tool:ask", "setting:tool:notify_user")
async def test_deleting_a_thread_forgets_every_key_that_names_its_sends(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port = bridge.stack.port_b
    route_name = uniq("l23-sends-route").replace("_", "-")
    execution_key = uniq("l23-sends-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    notice = uniq("l23-notice")
    reply = uniq("l23-reply")
    # The route sits on the deployment's own number, the identity an ask goes from, so the ask
    # lands in the same thread as the turn; a tool turn makes the mid-turn ``notify_user``.
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="notify_user",
        execution_key=execution_key,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        start_expr=f'{{message: "{notice}", channel: "twilio", recipient: .sender}}',
        reply_expr=f'"{reply}"',
    )
    try:

        async def twilio_message(text: str) -> None:
            inbound = bridge.twilio_inbound(
                our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text=text, port=port
            )
            assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204

        await twilio_message("hello")
        sids = [(await wait_twilio_send(bridge.fake_twilio, text))["sid"] for text in (notice, reply)]

        question = uniq("l23-question")
        answer = uniq("l23-answer")

        async def ask() -> object:
            async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
                return (await mcp.call_tool("ask", {"question": question, "channel": "twilio"})).data.answer

        task = asyncio.create_task(ask())
        try:
            sids.append((await wait_twilio_send(bridge.fake_twilio, question))["sid"])
            await twilio_message(answer)
            assert await asyncio.wait_for(task, timeout=15.0) == answer
        finally:
            await cancel_and_join(task)

        thread_id = f"bridge:{route_name}:{BRIDGE_TWILIO_CLIENT}"
        items = (await bridge.api().get(_transcript_path(route_name, thread_id)))["items"]
        assert sorted((item["origin"], item["sent_by"]) for item in items) == [
            ("client", None),
            ("send", "ask"),
            ("send", "notify"),
        ]
        (asked,) = [item for item in items if item["sent_by"] == "ask"]
        needles = [*(item["message_id"] for item in items), *sids, asked["ask"]["interaction_id"]]
        assert redis_keys_naming(bridge, needles), "the scan found no key naming the thread's messages"

        removed = await bridge.api().delete(_thread_delete_path(route_name, thread_id))
        assert removed["removed"] == 3

        await bridge.api().get(_transcript_path(route_name, thread_id), expect=404)
        assert redis_keys_naming(bridge, needles) == {}
    finally:
        await bridge.api().delete(f"/api/conversations/{route_name}")
