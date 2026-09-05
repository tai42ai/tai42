"""The event door — a structured event enters an existing thread as a turn.

``POST /api/conversations/{route}/events`` runs a structured event AS A TURN on a thread a
guest already composed: it reserves the same thread slot an inbound message takes, runs the
route's TOOL target under the route's execution key, and delivers the reply back over the
target route's door. The event is idempotent on its ``event_id`` — a redelivery starts no
second turn and adds no transcript entry.

The web channel is the medium here because it has no vendor: the visitor's own SSE stream is
the client-side surface an event's reply either does or does not reach. The route's tool
echoes the surfaced ``turn``/``event`` payload keys (§3.2) back as a JSON reply, so the
guest's transcript is proof the keys reached the flow with the event's own inbound identity.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness

_AGENT = "tools_agent"

# The tool echoes the surfaced turn/event keys back as a JSON reply string. e2e_echo returns
# ``payload`` unchanged, so the visitor's transcript carries exactly this object — the proof
# that ``.turn`` and ``.event`` reached the tool payload with the event's own inbound identity.
_ECHO_EXPR = (
    "{payload: ("
    "{turn_id: .turn.id,"
    " inbound_id: .turn.inbound.id,"
    " inbound_kind: .turn.inbound.kind,"
    " inbound_source: .turn.inbound.source,"
    " event_id: (.event.id // null),"
    " event_kind: (.event.kind // null),"
    " event_ref: (.event.payload.ref // null)}"
    " | tojson)}"
)


def _base_url(bridge: BridgeHarness) -> str:
    return f"http://{bridge.stack.host}:{bridge.stack.port_b}"


def _reply_matching(**expect: object) -> Callable[[str, dict], bool]:
    """A predicate for an outbound chat frame whose JSON reply body carries every ``expect``
    key/value. The tool's reply is a JSON string, so a frame's ``text`` is decoded and
    matched field by field; a non-JSON frame (never produced by this route) never matches."""

    def predicate(event: str, data: dict) -> bool:
        if event != "chat.message" or data.get("direction") != "out":
            return False
        try:
            body = json.loads(data["text"])
        except (ValueError, KeyError, TypeError):
            return False
        return all(body.get(key) == value for key, value in expect.items())

    return predicate


async def _web_tool_route(bridge: BridgeHarness, uniq: Callable[[str], str], tag: str) -> tuple[str, str]:
    """Create a ``target_kind=tool`` web route whose tool echoes the surfaced turn/event keys
    as a JSON reply; returns ``(route name, our_identity)``."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    exec_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_echo",
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        payload_expr=_ECHO_EXPR,
    )
    return route_name, identity


async def _open_visitor(bridge: BridgeHarness, identity: str) -> WebChatClient:
    web, page = await WebChatClient.open_page(_base_url(bridge), identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web


async def _out_replies(web: WebChatClient) -> list[dict]:
    """Every outbound reply body in the visitor's transcript replay, decoded — the census a
    "no new turn" idempotency assertion deltas against."""
    replayed = await web.frames()
    out: list[dict] = []
    for event, data in replayed:
        if event == "chat.message" and data.get("direction") == "out":
            out.append(json.loads(data["text"]))
    return out


async def _listed_thread(bridge: BridgeHarness, route_name: str, expect_address: str) -> str:
    """The route's single thread id, checked to be the visitor's own route-keyed thread."""
    listing = await bridge.api().get(f"/api/conversations/{route_name}/threads")
    (thread,) = listing["items"]
    assert thread["client_address"] == expect_address, thread
    return thread["thread_id"]


async def test_event_runs_as_a_turn_and_surfaces_its_inbound_id_on_the_guest_thread(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    route_name, identity = await _web_tool_route(bridge, uniq, "evt")
    web = await _open_visitor(bridge, identity)

    # 1. The guest composes the thread with an ordinary message; its turn is a ``message``
    #    inbound, so the tool sees ``turn.inbound.kind == "message"`` and no ``event``.
    sent = await web.send(uniq("evt-in"))
    assert sent.status_code == 200, sent.text
    message_frames = await web.frames(until=_reply_matching(inbound_kind="message"), deadline=40.0)
    message_reply = json.loads(message_frames[-1][1]["text"])
    assert message_reply["inbound_source"] == "web"
    assert message_reply["event_kind"] is None

    thread_id = await _listed_thread(bridge, route_name, web.visitor_id)

    # 2. An event addressed by the guest's address runs a TOOL turn on that same thread; its
    #    reply reaches the guest's stream carrying the event kind and the event's OWN inbound
    #    id/kind, and ``turn.id`` is the accepted record's message id.
    accepted = await bridge.api().post(
        f"/api/conversations/{route_name}/events",
        json={
            "address": web.visitor_id,
            "event": {"event_id": "evt-1", "kind": "provider.update", "payload": {"ref": "A-42"}},
        },
        expect=202,
    )
    event_message_id = accepted["message_id"]
    assert accepted["thread_id"] == thread_id

    event_frames = await web.frames(until=_reply_matching(inbound_kind="event", event_id="evt-1"), deadline=40.0)
    event_reply = json.loads(event_frames[-1][1]["text"])
    assert event_reply["event_kind"] == "provider.update"
    assert event_reply["event_ref"] == "A-42"
    assert event_reply["inbound_id"] == "evt-1"
    assert event_reply["inbound_kind"] == "event"
    assert event_reply["inbound_source"] == "event:provider.update"
    assert event_reply["turn_id"] == event_message_id

    # 3. Idempotency — the SAME ``event_id`` redelivered returns the original ``message_id``
    #    and runs no second turn, so the transcript gains no new outbound.
    out_before = await _out_replies(web)
    redelivered = await bridge.api().post(
        f"/api/conversations/{route_name}/events",
        json={
            "address": web.visitor_id,
            "event": {"event_id": "evt-1", "kind": "provider.update", "payload": {"ref": "A-42"}},
        },
        expect=202,
    )
    assert redelivered["message_id"] == event_message_id
    out_after = await _out_replies(web)
    assert out_after == out_before, "a redelivered event started a second turn"

    # 4. ``thread_id`` addressing — the SAME thread reached by its listed id (not its address)
    #    delivers the reply just the same.
    by_thread = await bridge.api().post(
        f"/api/conversations/{route_name}/events",
        json={
            "thread_id": thread_id,
            "event": {"event_id": "evt-2", "kind": "provider.update", "payload": {"ref": "A-99"}},
        },
        expect=202,
    )
    assert by_thread["thread_id"] == thread_id
    by_thread_frames = await web.frames(until=_reply_matching(inbound_kind="event", event_id="evt-2"), deadline=40.0)
    by_thread_reply = json.loads(by_thread_frames[-1][1]["text"])
    assert by_thread_reply["event_ref"] == "A-99"
    assert by_thread_reply["turn_id"] == by_thread["message_id"]


async def test_event_door_refuses_an_unknown_thread_and_an_agent_target(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    # An address that composes no existing thread is a 404 — an event never mints a thread.
    tool_route, _identity = await _web_tool_route(bridge, uniq, "evt-404")
    await bridge.api().post(
        f"/api/conversations/{tool_route}/events",
        json={
            "address": uniq("evt-ghost").replace("_", "-"),
            "event": {"event_id": uniq("evt-miss"), "kind": "provider.update", "payload": {}},
        },
        expect=404,
    )

    # An agent-target route is a 409 — an event carries no rendered text to hand an agent. The
    # refusal precedes thread resolution, so it needs no composed thread.
    agent_route = uniq("evt-agent-route").replace("_", "-")
    agent_identity = uniq("evt-agent-site").replace("_", "-")
    agent_exec = uniq("evt-agent-exec")
    await bridge.mint_key(user_id=agent_exec, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=agent_route,
        agent=_AGENT,
        execution_key=agent_exec,
        channel="web",
        our_identity=agent_identity,
    )
    await bridge.api().post(
        f"/api/conversations/{agent_route}/events",
        json={
            "address": uniq("evt-any").replace("_", "-"),
            "event": {"event_id": uniq("evt-agent"), "kind": "provider.update", "payload": {}},
        },
        expect=409,
    )
