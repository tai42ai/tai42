"""A tool target's terminal failure that carries a participant-facing reply, end to end on the real stack.

A target that fails with ``RunTerminalFailed(outcome, reply=...)`` has its ``reply`` shown to the
visitor in place of the conversation door's fixed failure line; the internal ``outcome`` never
reaches the visitor (a live turn keeps it in the record's admin-only ``error`` field):

* a LIVE turn: a ``target_kind=tool`` web route whose tool raises the failure at start;
* a RESUMED turn: a tool-target park answered out of band on another worker, whose resume
  continuation raises the failure, delivered back through ``deliver_tool_completion`` into the
  completion record it mints.

Generic throughout: a plain probe tool with no driver of its own.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.webchat import WebChatClient

from ._bridge_support import (
    ERROR_ANSWER_TEXT,
    BridgeHarness,
    open_tool_target_web_visitor,
    outbound_message,
    park_tool_target_message,
    wait_probe_record,
)

# The sentinel answer and reply the ``e2e_tool_target_deliver`` fixture reads as a NON-SUCCESS
# terminal carrying its own reply. Kept in lockstep with the fixture's own constants — a drift
# shows up loudly here (the resume would map the sentinel as a success, or send another text).
_ABORT_REPLY_ANSWER = "__abort_reply__"
_ABORT_REPLY = "That request could not be completed. Please start again."

pytestmark = pytest.mark.needs(
    "kind:channels:web",
    "kind:identity",
    "probe-tools",
    "setting:conversations",
    "setting:seeded-access-control",
    "store:redis",
    "topology:replicas",
)


def _out_texts(frames: list[tuple[str, dict]]) -> list[str]:
    return [data["text"] for event, data in frames if event == "chat.message" and data["direction"] == "out"]


async def _record_answering(bridge: BridgeHarness, route_name: str, answer: str) -> dict:
    """The one admin record on ``route_name`` whose answer is ``answer``."""
    hits = await bridge.api().get(f"/api/conversations/{route_name}/messages/search?{urlencode({'q': answer})}")
    records = [item for item in hits["items"] if item["answer"] == answer]
    assert len(records) == 1, f"expected one record answering {answer!r}, got {hits['items']!r}"
    return records[0]


async def test_a_live_tool_turn_failing_with_a_reply_shows_the_visitor_the_reply(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = uniq("ttreply-site").replace("_", "-")
    route_name = uniq("ttreply-route").replace("_", "-")
    execution_key = uniq("ttreply-exec")
    reason = uniq("ttreply-internal-detail")
    reply = f"Sorry, that option is no longer available ({uniq('ttreply')}). Pick another?"
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_failed_terminal_reply",
        execution_key=execution_key,
        channel="web",
        our_identity=identity,
        start_expr=f"{{reason: {json.dumps(reason)}, reply: {json.dumps(reply)}}}",
    )
    base_url = bridge.stack.origin(bridge.stack.port_b)
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text

    sent = await web.send(uniq("ttreply-msg"))
    assert sent.status_code == 200, sent.text

    # The visitor reads the target's own reply, never the door's fixed line nor the internal reason.
    out_texts = _out_texts(await web.frames(until=outbound_message(reply), deadline=40.0))
    assert reply in out_texts, f"the target's reply was not delivered, saw {out_texts!r}"
    assert not any(ERROR_ANSWER_TEXT in text for text in out_texts), out_texts
    assert not any(reason in text for text in out_texts), f"the internal reason leaked: {out_texts!r}"

    # The admin record keeps the internal detail in its error field.
    record = await _record_answering(bridge, route_name, reply)
    assert record["answer_status"] == "error"
    assert reason in record["error"]


async def test_a_resumed_tool_turn_failing_with_a_reply_shows_the_visitor_the_reply(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    web, route_name = await open_tool_target_web_visitor(bridge, uniq, "ttabortreply")
    marker = uniq("ttabortreply-msg")
    interaction_id = await park_tool_target_message(bridge, web, marker)

    # Answer the park out of band on replica A with the abort-with-reply sentinel: the resume raises
    # a failure carrying its own reply, and the ladder delivers it FAILED.
    answered = await bridge.api(port=bridge.stack.port_a).post(
        f"/api/interactions/{interaction_id}/answer", json={"answer": _ABORT_REPLY_ANSWER}
    )
    assert answered["status"] == "answered"
    fired = await wait_probe_record(bridge, f"tool_target_deliver:{interaction_id}", deadline=20.0)
    assert fired[0]["status"] == "failed"

    out_texts = _out_texts(await web.frames(until=outbound_message(_ABORT_REPLY), deadline=40.0))
    assert _ABORT_REPLY in out_texts, f"the resumed failure's reply was not delivered, saw {out_texts!r}"
    assert not any(ERROR_ANSWER_TEXT in text for text in out_texts), out_texts
    assert not any(_ABORT_REPLY_ANSWER in text for text in out_texts), out_texts

    # The completion record the ladder minted answers with the reply, never the internal reason.
    record = await _record_answering(bridge, route_name, _ABORT_REPLY)
    assert "aborted" not in record["answer"]
