"""Overlap door: manual mode under ``deliver=all`` appends every message of the batch to memory.

A manual-mode thread runs no target turn, but an agent target that holds thread memory still has
the inbound appended to its checkpoint. The manual append runs on the WHOLE turn ``overlap.turn_text``
computes — under ``deliver=all`` the superseded texts then the batch texts, in acceptance order — so no
message is lost to memory, whether it merged or was superseded:

* three messages that batch inside a settle window all reach the agent's memory (the merge case), and
* a first message whose turn is CANCELLED by a newer one rides the surviving batch turn's ``superseded``
  into the same append, so its text reaches memory ahead of the batch texts (the supersede case).

Both are proven by a later (agent-mode) resume turn whose model request replays the appended memory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness, cancel_and_join, request_mentions, script_reply, wait_record_status
from ._overlap_support import joined, open_visitor, reply_matching, send_web

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted-LLM is the 'llm' mock leg; the real leg runs on the creds host",
)

_AGENT = "tools_agent"
_SETTLE_SECONDS = 2
# Holds message 1's held agent turn open on its model call long enough for the newer messages to cancel it.
_HELD_SECONDS = 30.0


def _request_content_mentions(llm_request: dict, needle: str) -> bool:
    """Whether any message content in a recorded model request contains ``needle`` as a real string, so a
    joined text with its literal blank-line joins is matched, not its escaped JSON form."""
    contents = [m.get("content") for m in llm_request.get("messages", []) if isinstance(m.get("content"), str)]
    return any(needle in content for content in contents)


async def _manual_agent_route(bridge: BridgeHarness, uniq: Callable[[str], str]) -> tuple[str, str]:
    identity = uniq("ov-manual-site").replace("_", "-")
    route_name = uniq("ov-manual-route").replace("_", "-")
    exec_key = uniq("ov-manual-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent=_AGENT,
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        initial_mode="manual",
        overlap={"deliver": "all", "settle_seconds": _SETTLE_SECONDS},
    )
    return route_name, identity


async def test_manual_mode_appends_every_message_of_the_batch(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    route_name, identity = await _manual_agent_route(bridge, uniq)
    web: WebChatClient = await open_visitor(bridge, identity)

    t1, t2, t3 = uniq("ov-m1"), uniq("ov-m2"), uniq("ov-m3")
    resume_text = uniq("ov-resume-in")
    resume_answer = uniq("ov-resume-ans")
    # Only the agent-mode resume turn runs a model turn; the three manual messages run none.
    script_reply(bridge.llm_stub, resume_answer)

    # 1. Three manual messages batch inside the settle window into ONE suppressed append: the lead
    #    is silent, the followers merged, and all three are appended to the agent's memory.
    id1 = await send_web(web, t1)
    id2 = await send_web(web, t2)
    id3 = await send_web(web, t3)
    r1 = await wait_record_status(bridge, route_name, id1, {"silent"})
    assert r1["answer"] is None
    for follower_id in (id2, id3):
        follower = await wait_record_status(bridge, route_name, follower_id, {"merged"})
        assert follower["successor_id"] == id1

    # 2. Flip the thread back to agent mode.
    (thread,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
    thread_id = thread["thread_id"]
    await bridge.api().put(
        f"/api/conversations/{route_name}/thread/mode", json={"thread_id": thread_id, "mode": "agent"}
    )

    # 3. A resume turn runs an agent turn whose request carries all three manual-period messages —
    #    every batch member reached memory through the append.
    tail = asyncio.create_task(web.frames(until=reply_matching(resume_answer), deadline=45.0))
    try:
        await send_web(web, resume_text)
        await asyncio.wait_for(tail, timeout=50.0)
    finally:
        await cancel_and_join(tail)

    assert len(bridge.llm_stub.requests) == 1, bridge.llm_stub.requests
    for text in (t1, t2, t3):
        assert request_mentions(bridge.llm_stub, 0, text), (
            f"the resume turn did not see manual-period message {text!r}; a batch member was lost to memory"
        )


async def test_manual_mode_carries_a_cancelled_message_into_memory(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Under ``running=cancel`` + ``deliver=all``, message 1's turn is cancelled and messages 2+3 form the
    batch: the surviving manual-mode turn appends the WHOLE ``overlap.turn_text`` — the superseded text then
    the batch texts — so the cancelled message's text is not lost from memory. The route starts in AGENT
    mode so message 1's turn holds on its model call (the only holdable seam), is flipped to MANUAL before
    the batch turn runs, and the memory is proven by a later agent-mode resume turn."""
    identity = uniq("ov-manual-cancel-site").replace("_", "-")
    route_name = uniq("ov-manual-cancel-route").replace("_", "-")
    exec_key = uniq("ov-manual-cancel-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent=_AGENT,
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        overlap={"running": "cancel", "deliver": "all", "settle_seconds": _SETTLE_SECONDS},
    )
    web: WebChatClient = await open_visitor(bridge, identity)

    t1, t2, t3 = uniq("ov-m1"), uniq("ov-m2"), uniq("ov-m3")
    resume_text = uniq("ov-resume-in")
    resume_answer = uniq("ov-resume-ans")
    hold_answer = uniq("ov-hold-ans")
    # Message 1's held agent turn pops the first scripted turn; the agent-mode resume turn pops the second.
    # The manual batch turn (messages 2+3) runs no model turn, so it draws none.
    script_reply(bridge.llm_stub, hold_answer, resume_answer)

    # Hold message 1's model call open so its turn is in flight — past the batch gather, its cancel watcher
    # armed — when the newer messages arrive.
    bridge.llm_stub.set_response_delay(_HELD_SECONDS)
    try:
        id1 = await send_web(web, t1)

        async def _turn_holds() -> bool:
            return len(bridge.llm_stub.requests) >= 1

        await wait_for_async(
            _turn_holds, deadline=20.0, message="message 1's agent turn never reached its held model call"
        )

        # Flip to manual BEFORE the batch turn runs. The surviving lead reads this mode when its turn runs
        # (after message 1 releases the FIFO), so it appends instead of running a model turn.
        (thread,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
        thread_id = thread["thread_id"]
        await bridge.api().put(
            f"/api/conversations/{route_name}/thread/mode", json={"thread_id": thread_id, "mode": "manual"}
        )

        # Messages 2 and 3: the newer marker cancels the held message 1; message 2 leads and message 3 merges.
        id2 = await send_web(web, t2)
        id3 = await send_web(web, t3)

        # Message 1 is superseded (its successor rides the surviving batch); message 2's manual turn is silent
        # and message 3 merged into it.
        r1 = await wait_record_status(bridge, route_name, id1, {"superseded"})
        assert r1["answer_status"] is None
        assert r1["successor_id"] in (id2, id3)
        r2 = await wait_record_status(bridge, route_name, id2, {"silent"})
        assert r2["answer"] is None
        r3 = await wait_record_status(bridge, route_name, id3, {"merged"})
        assert r3["successor_id"] == id2
    finally:
        bridge.llm_stub.set_response_delay(0.0)

    # Flip back to agent and resume: the model turn replays the checkpoint, which now carries the whole
    # cancelled-then-batched turn as one appended user message — the superseded text first, then the batch.
    await bridge.api().put(
        f"/api/conversations/{route_name}/thread/mode", json={"thread_id": thread_id, "mode": "agent"}
    )
    tail = asyncio.create_task(web.frames(until=reply_matching(resume_answer), deadline=45.0))
    try:
        await send_web(web, resume_text)
        await asyncio.wait_for(tail, timeout=50.0)
    finally:
        await cancel_and_join(tail)

    resume_request = bridge.llm_stub.requests[-1]
    for text in (t1, t2, t3):
        assert request_mentions(bridge.llm_stub, len(bridge.llm_stub.requests) - 1, text), (
            f"the resume turn did not see message {text!r}; the cancelled-then-batched turn was lost to memory"
        )
    assert _request_content_mentions(resume_request, joined(t1, t2, t3)), (
        "the manual turn did not append the whole cancelled-then-batched turn as one joined message"
    )
