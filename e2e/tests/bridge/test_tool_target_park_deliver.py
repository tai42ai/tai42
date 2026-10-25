"""A CONVERSATION tool-target route whose tool ASYNC-PARKS, is answered out of band on
another worker, and whose resumed reply is delivered back into the originating web transcript.

This is the unified park-completion binding's core new path, end to end on the real stack:

* A ``target_kind=tool`` web route dispatches ``e2e_tool_target_park`` per inbound message. That
  tool does NOT bind its own probe continuation — it reads the ``deliver_tool_completion``
  binding the conversation door's REAL ``_run_tool_turn`` pins around the dispatch (the delivery
  address carrying the thread and the opaque ``{route_name, message_id}`` context), binds a
  resume continuation, and async-parks on ``ask(mode="async")``. So the PRODUCTION completion
  continuation is what carries the deferred reply back.
* A visitor's message parks the turn SILENTLY — no synchronous reply is posted, the interaction
  is persisted — mirroring the web round-trip harness of ``test_web_public_chat``.
* The park is answered OUT OF BAND through a DIFFERENT replica's interactions door (the
  cross-worker resume of ``test_async_park_resume``). Its stored continuation fires the REAL
  ``deliver_tool_completion``, which maps the terminal through the route's ``reply_expr`` and
  delivers the reply back into the visitor's transcript, where a reconnect replays it.
* Re-firing the same ``completion_id`` (an at-least-once redelivery) does NOT double-post: the
  delivery record is keyed on it.
* The non-success leg (an aborted/failed resume) delivers the route's UNIFORM client-safe error
  notice — not a mapped reply, not silence.

Generic throughout: the capability under test is the ask / tool-target / deliver-back seam,
never a flow or engine.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from ._bridge_support import (
    ERROR_ANSWER_TEXT,
    BridgeHarness,
    open_tool_target_web_visitor,
    outbound_message,
    park_tool_target_message,
    wait_probe_record,
)

# The sentinel answer the ``e2e_tool_target_deliver`` fixture reads as a NON-SUCCESS terminal (an
# aborted/failed resume): it drives ``deliver_tool_completion`` with the ``failed`` status. Kept
# in lockstep with the fixture's own constant — a drift shows up loudly here (the resume would map
# it as a success reply, and the error-notice assertion below would fail).
_ABORT_ANSWER = "__abort__"


pytestmark = pytest.mark.needs(
    "kind:channels:web",
    "kind:identity",
    "probe-tools",
    "setting:conversations",
    "setting:seeded-access-control",
    "store:redis",
    "topology:replicas",
)


async def test_tool_target_park_delivers_its_resumed_reply_back_to_the_conversation(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    # The reply carries the late turn's ``$turn.reply_window`` beside the answer: the web channel
    # has no reply window, so the late reply reads ``none``.
    web, _route_name = await open_tool_target_web_visitor(
        bridge, uniq, "ttdeliver", reply_expr="{answer: .result.answer, reply_window: $turn.reply_window} | tojson"
    )
    marker = uniq("ttdeliver-msg")
    interaction_id = await park_tool_target_message(bridge, web, marker)

    # Answer the park OUT OF BAND through replica A's interactions door — a DIFFERENT worker than
    # the one that ran the web turn on replica B. That door claims the answer and fires the stored
    # continuation, which drives the REAL deliver_tool_completion.
    answer = uniq("ttdeliver-ans")
    answered = await bridge.api(port=bridge.stack.port_a).post(
        f"/api/interactions/{interaction_id}/answer", json={"answer": answer}
    )
    assert answered["status"] == "answered"

    # The resume fired its clean-success terminal, and the reply — the reply_expr-mapped answer,
    # not the raw result — is delivered back into the visitor's transcript.
    fired = await wait_probe_record(bridge, f"tool_target_deliver:{interaction_id}", deadline=20.0)
    assert fired[0]["status"] == "succeeded"
    delivered = await web.frames(until=outbound_message(answer), deadline=40.0)
    out_texts = [data["text"] for event, data in delivered if event == "chat.message" and data["direction"] == "out"]
    assert any(answer in text for text in out_texts), f"the resumed reply was not delivered, saw {out_texts!r}"
    (late_reply,) = [json.loads(text) for text in out_texts if answer in text]
    assert late_reply == {"answer": answer, "reply_window": {"state": "none", "closes_at": None}}

    # A resumer call OUTSIDE the platform's delivery ladder produces the terminal but delivers
    # NOTHING: an address tool fires only inside the ladder's delivery-fire context. Re-firing the
    # continuation directly returns the same terminal and posts no second reply.
    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        again = await mcp.call_tool(
            "e2e_tool_target_deliver",
            {"interaction_id": interaction_id, "ask_result": {"answer": answer, "reply_to": None, "params": None}},
        )
    assert again.data["result"]["answer"] == answer

    replay = await web.frames()
    delivered_replies = [
        data["text"]
        for event, data in replay
        if event == "chat.message" and data["direction"] == "out" and answer in data["text"]
    ]
    assert len(delivered_replies) == 1, f"a redelivered completion double-posted the reply: {delivered_replies!r}"


async def test_tool_target_park_non_success_resume_delivers_the_error_notice(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    web, _route_name = await open_tool_target_web_visitor(bridge, uniq, "ttabort")
    marker = uniq("ttabort-msg")
    interaction_id = await park_tool_target_message(bridge, web, marker)

    # Resolve the park with the abort sentinel: the resume drives a NON-SUCCESS terminal, so the
    # route (which carries no error mapping) delivers its UNIFORM client-safe notice.
    answered = await bridge.api(port=bridge.stack.port_a).post(
        f"/api/interactions/{interaction_id}/answer", json={"answer": _ABORT_ANSWER}
    )
    assert answered["status"] == "answered"

    fired = await wait_probe_record(bridge, f"tool_target_deliver:{interaction_id}", deadline=20.0)
    assert fired[0]["status"] == "failed"

    # The delivered reply is the client-safe error notice — never a mapped reply, never silence.
    delivered = await web.frames(until=outbound_message(ERROR_ANSWER_TEXT), deadline=40.0)
    out_texts = [data["text"] for event, data in delivered if event == "chat.message" and data["direction"] == "out"]
    assert any(ERROR_ANSWER_TEXT in text for text in out_texts), f"no error notice was delivered, saw {out_texts!r}"
    # The abort sentinel never leaked back as a reply (it was not mapped through reply_expr).
    assert not any(_ABORT_ANSWER in text for text in out_texts), (
        f"the abort terminal leaked a mapped reply: {out_texts!r}"
    )


async def test_tool_target_park_non_success_resume_delivers_the_routes_own_error_reply(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    error_reply = "The assistant could not finish that one."
    web, _route_name = await open_tool_target_web_visitor(bridge, uniq, "tterrtext", reply_texts={"error": error_reply})
    marker = uniq("tterrtext-msg")
    interaction_id = await park_tool_target_message(bridge, web, marker)

    # A NON-SUCCESS terminal on a route that words its error reply delivers the route's sentence
    # in place of the built-in notice.
    answered = await bridge.api(port=bridge.stack.port_a).post(
        f"/api/interactions/{interaction_id}/answer", json={"answer": _ABORT_ANSWER}
    )
    assert answered["status"] == "answered"
    fired = await wait_probe_record(bridge, f"tool_target_deliver:{interaction_id}", deadline=20.0)
    assert fired[0]["status"] == "failed"

    delivered = await web.frames(until=outbound_message(error_reply), deadline=40.0)
    out_texts = [data["text"] for event, data in delivered if event == "chat.message" and data["direction"] == "out"]
    assert error_reply in out_texts, f"the route's error reply was not delivered, saw {out_texts!r}"
    assert not any(ERROR_ANSWER_TEXT in text for text in out_texts), out_texts
