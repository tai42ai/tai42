"""A CONVERSATION tool-target route whose tool ASYNC-PARKS, is answered out of band on
another worker, and whose resumed reply is delivered back into the originating web transcript.

This is the unified park-completion binding's core new path, end to end on the real stack:

* A ``target_kind=tool`` web route dispatches ``e2e_tool_target_park`` per inbound message. That
  tool does NOT bind its own probe continuation — it reads the ``deliver_tool_completion``
  binding the conversation door's REAL ``_run_tool_turn`` pins around the dispatch (the opaque
  ``{delivery_thread_id, route_name}`` context), binds a resume continuation, and async-parks on
  ``ask_user(mode="async")``. So the PRODUCTION completion continuation is what carries the
  deferred reply back.
* A visitor's message parks the turn SILENTLY — no synchronous reply is posted, the interaction
  is persisted — mirroring the web round-trip harness of ``test_l12``.
* The park is answered OUT OF BAND through a DIFFERENT replica's interactions door (the
  cross-worker resume of ``test_async_park_resume``). Its stored continuation fires the REAL
  ``deliver_tool_completion``, which maps the terminal through the route's ``reply_expr`` and
  delivers the reply back into the visitor's transcript, where a reconnect replays it.
* Re-firing the same ``completion_id`` (an at-least-once redelivery) does NOT double-post: the
  delivery record is keyed on it.
* The non-success leg (an aborted/failed resume) delivers the route's UNIFORM client-safe error
  notice — not a mapped reply, not silence.

Generic throughout: the capability under test is the ask_user / tool-target / deliver-back seam,
never a flow or engine.
"""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e.webchat import WebChatClient

from ._bridge_support import ERROR_ANSWER_TEXT, BridgeHarness, wait_probe_record

# The sentinel answer the ``e2e_tool_target_deliver`` fixture reads as a NON-SUCCESS terminal (an
# aborted/failed resume): it drives ``deliver_tool_completion`` with the ``failed`` status. Kept
# in lockstep with the fixture's own constant — a drift shows up loudly here (the resume would map
# it as a success reply, and the error-notice assertion below would fail).
_ABORT_ANSWER = "__abort__"


def _outbound(text: str) -> Callable[[str, dict], bool]:
    """Predicate for the delivered reply arriving on the visitor's stream."""
    return lambda event, data: event == "chat.message" and data["direction"] == "out" and text in data["text"]


async def _open_tool_target_web_visitor(bridge: BridgeHarness, uniq: Callable[[str], str], tag: str) -> WebChatClient:
    """Create a ``target_kind=tool`` web route onto ``e2e_tool_target_park`` (message → ``marker``
    kwarg; resumed outcome → ``.result.answer`` reply) and open its chat page as a first-time
    visitor, returning the door client bound to the session the page minted."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    execution_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_tool_target_park",
        execution_key=execution_key,
        channel="web",
        our_identity=identity,
        payload_expr="{marker: .message}",
        reply_expr=".result.answer",
    )
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


async def _park_a_message(bridge: BridgeHarness, web: WebChatClient, marker: str) -> str:
    """Send ``marker`` into the visitor's conversation, assert the tool turn PARKED SILENTLY (the
    park barrier lands and the transcript holds only the inbound message, no reply), and return
    the parked ``interaction_id``."""
    sent = await web.send(marker)
    assert sent.status_code == 200, sent.text

    # The park barrier: the tool ran to the async park and stashed its interaction id under the
    # marker. For a silent turn (no outbound send, no readable channel id) this side effect is
    # the completion barrier the turn otherwise lacks.
    parked = await wait_probe_record(bridge, f"tool_target_park:{marker}", deadline=20.0)
    assert len(parked) == 1, f"expected exactly one park record, got {parked!r}"
    interaction_id = parked[0]["interaction_id"]

    # Silent: the parked turn posted nothing back — the transcript holds only the inbound message.
    replayed = await web.frames()
    exchange = [(data["direction"], data["text"]) for event, data in replayed if event == "chat.message"]
    assert [direction for direction, _text in exchange] == ["in"], f"a parked turn must post no reply, saw {exchange!r}"
    assert marker in exchange[0][1]
    return interaction_id


async def test_tool_target_park_delivers_its_resumed_reply_back_to_the_conversation(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    web = await _open_tool_target_web_visitor(bridge, uniq, "ttdeliver")
    marker = uniq("ttdeliver-msg")
    interaction_id = await _park_a_message(bridge, web, marker)

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
    delivered = await web.frames(until=_outbound(answer), deadline=40.0)
    out_texts = [data["text"] for event, data in delivered if event == "chat.message" and data["direction"] == "out"]
    assert any(answer in text for text in out_texts), f"the resumed reply was not delivered, saw {out_texts!r}"

    # IDEMPOTENT: re-fire the resume out of band with the SAME interaction/answer (an
    # at-least-once redelivery). deliver_tool_completion is keyed on the completion_id (the
    # interaction id), so the already-committed record is a benign no-op — no second reply posts.
    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        again = await mcp.call_tool("e2e_tool_target_deliver", {"interaction_id": interaction_id, "answer": answer})
    assert again.data["status"] == "succeeded"

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
    web = await _open_tool_target_web_visitor(bridge, uniq, "ttabort")
    marker = uniq("ttabort-msg")
    interaction_id = await _park_a_message(bridge, web, marker)

    # Resolve the park with the abort sentinel: the resume drives a NON-SUCCESS terminal, so the
    # route (which carries no error mapping) delivers its UNIFORM client-safe notice.
    answered = await bridge.api(port=bridge.stack.port_a).post(
        f"/api/interactions/{interaction_id}/answer", json={"answer": _ABORT_ANSWER}
    )
    assert answered["status"] == "answered"

    fired = await wait_probe_record(bridge, f"tool_target_deliver:{interaction_id}", deadline=20.0)
    assert fired[0]["status"] == "failed"

    # The delivered reply is the client-safe error notice — never a mapped reply, never silence.
    delivered = await web.frames(until=_outbound(ERROR_ANSWER_TEXT), deadline=40.0)
    out_texts = [data["text"] for event, data in delivered if event == "chat.message" and data["direction"] == "out"]
    assert any(ERROR_ANSWER_TEXT in text for text in out_texts), f"no error notice was delivered, saw {out_texts!r}"
    # The abort sentinel never leaked back as a reply (it was not mapped through reply_expr).
    assert not any(_ABORT_ANSWER in text for text in out_texts), (
        f"the abort terminal leaked a mapped reply: {out_texts!r}"
    )
