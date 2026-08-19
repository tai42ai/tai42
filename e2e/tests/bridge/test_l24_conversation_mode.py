"""L24 — per-thread conversation control mode over the public web chat round trip.

A thread runs in one of two modes. In ``agent`` mode (the route default) an inbound client
message buys a target turn whose answer is delivered back. In ``manual`` mode the target
turn is SUPPRESSED: the inbound terminalizes ``silent`` on the channel door, no answer
goes out, and — for an agent target — the inbound is appended to the agent's thread memory
so a later agent turn reads it as prior context. Between the two an operator answers by
hand: ``POST /thread/messages`` mints an ``operator`` record, delivers it into the same
visitor surface as the route identity, and appends an ``assistant`` line to the thread's
memory. It works in either mode and never flips the mode.

The web channel is driven because it has no vendor: its own public doors ARE the medium, so
the visitor's SSE stream is the client-side surface an answer either does or does not reach.

The full loop rides one route and one visitor: an agent baseline turn, a flip to manual with
a suppressed turn, an operator send that surfaces, a flip back to agent whose turn's request
carries the manual-period exchange (the scripted-LLM continuity read L12/L16 use), with the
mode door read at each stage for its ``mode`` and its ``source``. A second route boots
``manual`` from the start, and the operator-send guards are asserted where cheap.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import (
    BridgeHarness,
    cancel_and_join,
    request_mentions,
    script_reply,
    wait_record_status,
)

# The scripted-LLM turns are the 'llm' mock leg; a real LLM breaks the scripting. The web
# channel itself has no vendor and so no real/mock split — it is always real.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted-LLM turns are the 'llm' mock leg; the real leg runs on the creds host",
)

_AGENT = "tools_agent"


def _outbound_message(text: str) -> Callable[[str, dict], bool]:
    """Predicate for an answer carrying ``text`` arriving on the visitor's live stream."""
    return lambda event, data: event == "chat.message" and data["direction"] == "out" and text in data["text"]


def _transcript_path(route_name: str, thread_id: str) -> str:
    """The admin transcript door URL: the thread id is a QUERY value, encoded once."""
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"


async def _web_route(
    bridge: BridgeHarness, uniq: Callable[[str], str], tag: str, *, initial_mode: str | None = None
) -> tuple[str, str]:
    """Create a web conversation route on its own identity and execution key; returns
    ``(route name, our_identity)``. ``initial_mode`` names the route's default mode when the
    leg wants one other than ``agent``."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    execution_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent=_AGENT,
        execution_key=execution_key,
        channel="web",
        our_identity=identity,
        initial_mode=initial_mode,
    )
    return route_name, identity


async def _open_visitor(bridge: BridgeHarness, identity: str) -> WebChatClient:
    """Open the chat page as a first-time visitor, adopting the session the page mints."""
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


async def _send_awaiting_out(web: WebChatClient, text: str, answer: str) -> str:
    """Send a client message and wait for ``answer`` to arrive on the visitor's stream,
    catching it whichever side of the concurrent open it lands on; returns the message id."""
    tail = asyncio.create_task(web.frames(until=_outbound_message(answer), deadline=40.0))
    try:
        sent = await web.send(text)
        assert sent.status_code == 200, sent.text
        message_id = sent.json()["data"]["message_id"]
        await asyncio.wait_for(tail, timeout=45.0)
        return message_id
    finally:
        await cancel_and_join(tail)


async def _thread_id(bridge: BridgeHarness, route_name: str, expect_address: str) -> str:
    """The route's single thread id, checked to be the visitor's own route-keyed thread."""
    listing = await bridge.api().get(f"/api/conversations/{route_name}/threads")
    (thread,) = listing["items"]
    assert thread["client_address"] == expect_address, thread
    return thread["thread_id"]


async def _replay_directions(web: WebChatClient) -> list[tuple[str, str]]:
    """The visitor's whole transcript replay as ``(direction, text)`` pairs, in order."""
    replayed = await web.frames()
    return [(data["direction"], data["text"]) for event, data in replayed if event == "chat.message"]


async def test_the_full_mode_loop_over_the_web_visitor_surface(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    route_name, identity = await _web_route(bridge, uniq, "l24")
    web = await _open_visitor(bridge, identity)

    baseline_text = uniq("l24-base-in")
    manual_text = uniq("l24-manual-in")
    operator_text = uniq("l24-operator")
    resumed_text = uniq("l24-resume-in")
    baseline_answer = uniq("l24-base-ans")
    resumed_answer = uniq("l24-resume-ans")
    # Two agent turns consume one scripted answer each: the baseline and the resumed turn.
    # The manual-period inbound and the operator send run no model turn, so they take none.
    script_reply(bridge.llm_stub, baseline_answer, resumed_answer)

    # 1. Baseline — the route default is ``agent``, sourced from the route (no override yet),
    #    and a client message buys a turn whose answer reaches the visitor's stream.
    await _send_awaiting_out(web, baseline_text, baseline_answer)
    thread_id = await _thread_id(bridge, route_name, web.visitor_id)
    default = await bridge.api().get(f"/api/conversations/{route_name}/thread/mode?thread_id={thread_id}")
    assert default == {"mode": "agent", "source": "route"}

    # 2. Flip to manual — the client message runs NO turn: its record terminalizes ``silent``
    #    and no answer goes out, while the client message itself still shows in the transcript.
    flipped = await bridge.api().put(
        f"/api/conversations/{route_name}/thread/mode", json={"thread_id": thread_id, "mode": "manual"}
    )
    assert flipped == {"route_name": route_name, "thread_id": thread_id, "mode": "manual", "source": "thread"}
    read_manual = await bridge.api().get(f"/api/conversations/{route_name}/thread/mode?thread_id={thread_id}")
    assert read_manual == {"mode": "manual", "source": "thread"}

    silent_sent = await web.send(manual_text)
    assert silent_sent.status_code == 200, silent_sent.text
    manual_id = silent_sent.json()["data"]["message_id"]
    manual_record = await wait_record_status(bridge, route_name, manual_id, {"silent"})
    assert manual_record["origin"] == "client"
    assert manual_record["inbound_text"] == manual_text
    assert manual_record["answer"] is None
    # The client message is in the visitor's replay as an inbound; nothing was answered, so
    # the only outbound so far is the baseline's — the suppressed turn added none.
    after_manual = await _replay_directions(web)
    assert (("in", manual_text)) in after_manual
    assert [direction for direction, _text in after_manual] == ["in", "out", "in"]

    # 3. Operator send — a by-hand reply reaches the same visitor surface as an ``out`` entry,
    #    carries ``origin=operator`` in the transcript, and its record reaches a sent state.
    tail = asyncio.create_task(web.frames(until=_outbound_message(operator_text), deadline=40.0))
    try:
        result = await bridge.api().post(
            f"/api/conversations/{route_name}/thread/messages",
            json={"thread_id": thread_id, "text": operator_text},
        )
        assert result["thread_id"] == thread_id
        operator_id = result["message_id"]
        await asyncio.wait_for(tail, timeout=45.0)
    finally:
        await cancel_and_join(tail)

    operator_record = await wait_record_status(bridge, route_name, operator_id, {"provisional", "delivered"})
    assert operator_record["origin"] == "operator"
    assert operator_record["answer"] == operator_text
    assert operator_record["inbound_text"] == ""
    assert operator_record["caller_principal"]
    transcript = await bridge.api().get(_transcript_path(route_name, thread_id))
    (operator_item,) = [item for item in transcript["items"] if item["message_id"] == operator_id]
    assert operator_item["origin"] == "operator"
    assert operator_item["answer"] == operator_text

    # 4. Flip back to agent — the override now sources ``agent`` from the thread (not the
    #    route), a client message buys a turn again, and that turn's request carries the whole
    #    manual-period exchange: the suppressed inbound AND the operator's reply are in the
    #    agent's memory. The scripted answer is fixed regardless, so the request is the proof.
    restored = await bridge.api().put(
        f"/api/conversations/{route_name}/thread/mode", json={"thread_id": thread_id, "mode": "agent"}
    )
    assert restored == {"route_name": route_name, "thread_id": thread_id, "mode": "agent", "source": "thread"}
    read_agent = await bridge.api().get(f"/api/conversations/{route_name}/thread/mode?thread_id={thread_id}")
    assert read_agent == {"mode": "agent", "source": "thread"}

    await _send_awaiting_out(web, resumed_text, resumed_answer)
    # Exactly two model turns ran — the baseline and this one — so the resumed turn is request
    # index 1, and it saw both manual-period lines as prior context.
    assert len(bridge.llm_stub.requests) == 2, bridge.llm_stub.requests
    assert request_mentions(bridge.llm_stub, 1, manual_text), (
        "the resumed agent turn did not see the manual-mode inbound; the suppressed message was not remembered"
    )
    assert request_mentions(bridge.llm_stub, 1, operator_text), (
        "the resumed agent turn did not see the operator's reply; the operator send did not enter thread memory"
    )
    # The manual-period inbound was NOT answered by an agent (request index 0 is the baseline,
    # which predates it), so the memory came from the append, not from a turn that ran.
    assert not request_mentions(bridge.llm_stub, 0, manual_text)


async def test_a_manual_from_the_start_route_is_silent_on_the_first_message(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    route_name, identity = await _web_route(bridge, uniq, "l24m", initial_mode="manual")
    web = await _open_visitor(bridge, identity)
    # No agent turn ever runs on this route, so the LLM script stays empty; an accidental turn
    # would fault the stub loudly rather than be served a default reply.

    first_text = uniq("l24m-in")
    sent = await web.send(first_text)
    assert sent.status_code == 200, sent.text
    record = await wait_record_status(bridge, route_name, sent.json()["data"]["message_id"], {"silent"})
    assert record["origin"] == "client"
    assert record["inbound_text"] == first_text
    assert record["answer"] is None

    # The first message shows as an inbound and nothing was answered — no outbound at all.
    directions = await _replay_directions(web)
    assert directions == [("in", first_text)]

    # The mode door reads ``manual`` sourced from the ROUTE default, no per-thread override.
    thread_id = await _thread_id(bridge, route_name, web.visitor_id)
    mode = await bridge.api().get(f"/api/conversations/{route_name}/thread/mode?thread_id={thread_id}")
    assert mode == {"mode": "manual", "source": "route"}


async def test_operator_send_guards(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    route_name, identity = await _web_route(bridge, uniq, "l24g")
    other_route, _other_identity = await _web_route(bridge, uniq, "l24g2")
    web = await _open_visitor(bridge, identity)

    # A real thread to send into, left behind by one baseline agent turn.
    answer = uniq("l24g-ans")
    script_reply(bridge.llm_stub, answer)
    await _send_awaiting_out(web, uniq("l24g-in"), answer)
    thread_id = await _thread_id(bridge, route_name, web.visitor_id)

    # Blank text is a loud 400 — an operator send carries a message, never an empty one.
    await bridge.api().post(
        f"/api/conversations/{route_name}/thread/messages",
        json={"thread_id": thread_id, "text": "   "},
        expect=400,
    )

    # A thread of another route is turned away: the route-keyed id carries THIS route's
    # ``bridge:{route}:`` prefix, so naming it on a different route is a 400, never a send
    # into a thread that route does not own.
    await bridge.api().post(
        f"/api/conversations/{other_route}/thread/messages",
        json={"thread_id": thread_id, "text": uniq("l24g-op")},
        expect=400,
    )
