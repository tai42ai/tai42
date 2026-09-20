"""Overlap door: an agent target under ``deliver=all`` receives the whole batch as one turn's text.

An agent target reads the rendered TEXT only — no ``messages`` structure — so ``deliver=all`` must
hand it the whole turn as one joined user message. Three messages inside a settle window ride ONE
agent turn whose model request carries the joined text, and the followers are ``merged``. This
proves the CARRY payload reaches an agent target with no new seam.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness, request_mentions, script_reply
from ._overlap_support import joined, open_visitor, reply_matching, send_web

# The scripted-LLM turn is the 'llm' mock leg; the real leg runs on the creds host.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted-LLM is the 'llm' mock leg; the real leg runs on the creds host",
)

_AGENT = "tools_agent"
_SETTLE_SECONDS = 2


async def _agent_route(
    bridge: BridgeHarness, uniq: Callable[[str], str], overlap: dict[str, object]
) -> tuple[str, str]:
    identity = uniq("ov-agent-site").replace("_", "-")
    route_name = uniq("ov-agent-route").replace("_", "-")
    exec_key = uniq("ov-agent-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent=_AGENT,
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        overlap=overlap,
    )
    return route_name, identity


def _request_content_mentions(web_stub_request: dict, needle: str) -> bool:
    """Whether any message content in a recorded model request equals-or-contains ``needle`` as a
    real string (so a joined text with its literal blank-line joins is matched, not the escaped
    JSON form)."""
    contents = [m.get("content") for m in web_stub_request.get("messages", []) if isinstance(m.get("content"), str)]
    return any(needle in content for content in contents)


async def test_an_agent_target_receives_the_whole_batch_as_one_joined_turn(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    route_name, identity = await _agent_route(bridge, uniq, {"deliver": "all", "settle_seconds": _SETTLE_SECONDS})
    answer = uniq("ov-agent-ans")
    # Exactly one agent turn runs — the batch turn — so one scripted answer; an accidental second
    # turn would fault the stub loudly.
    script_reply(bridge.llm_stub, answer)
    web: WebChatClient = await open_visitor(bridge, identity)

    t1, t2, t3 = uniq("ov-m1"), uniq("ov-m2"), uniq("ov-m3")
    id1 = await send_web(web, t1)
    id2 = await send_web(web, t2)
    id3 = await send_web(web, t3)

    await web.frames(until=reply_matching(answer), deadline=45.0)

    # Exactly one model turn ran, and its request carried the whole batch joined into one user
    # message — the agent target saw the whole turn.
    assert len(bridge.llm_stub.requests) == 1, bridge.llm_stub.requests
    for text in (t1, t2, t3):
        assert request_mentions(bridge.llm_stub, 0, text), f"the agent turn did not carry {text!r}"
    assert _request_content_mentions(bridge.llm_stub.requests[0], joined(t1, t2, t3)), (
        "the agent turn did not receive the batch as one joined text"
    )

    # The lead answered; the followers were merged into it.
    r1 = await bridge.get_record(route_name, id1)
    assert r1["answer_status"] == "answered"
    for follower_id in (id2, id3):
        follower = await bridge.get_record(route_name, follower_id)
        assert follower["delivery_status"] == "merged"
        assert follower["successor_id"] == id1
