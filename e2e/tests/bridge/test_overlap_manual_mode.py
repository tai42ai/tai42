"""Overlap door: manual mode under ``deliver=all`` appends every message of the batch to memory.

A manual-mode thread runs no target turn, but an agent target that holds thread memory still has
the inbound appended to its checkpoint. Under ``deliver=all`` every message the batch carries is
appended, in acceptance order, so no message is lost to memory: three messages that batch inside a
settle window all reach the agent's memory, proven by a later (agent-mode) turn whose request
carries all three. This proves the CARRY payload reaches the manual-mode append with no new seam.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness, cancel_and_join, request_mentions, script_reply, wait_record_status
from ._overlap_support import open_visitor, reply_matching, send_web

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted-LLM is the 'llm' mock leg; the real leg runs on the creds host",
)

_AGENT = "tools_agent"
_SETTLE_SECONDS = 2


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
