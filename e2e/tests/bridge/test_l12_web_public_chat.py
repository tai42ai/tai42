"""L12 — the public web chat round trip (no vendor, the plugin's own doors).

A visitor opens the chat page and is minted a ``tai_web_session`` cookie holding a SECRET
token; the server registers that token against an opaque ``visitor_id`` which is the
conversation's durable address and is never sent to the client. A message POSTed to the
public message door routes to the bridge under that address, runs a deterministic agent
turn, and the answer is delivered back into the same visitor transcript, where the
visitor's SSE stream receives it live. Reopening the stream replays the whole exchange in
order — the reconnect a browser does on every navigation.

The second leg proves the ask and the bridge share ONE conversation: an ``ask_user`` aimed
at the same ``(identity, visitor id)`` pair lands in that visitor's transcript alongside
the bridged messages and is answered from the page's own answer door.

The third leg is the spend fence: only the chat page mints AND registers a session, so a
well-formed cookie value invented by a caller opens no conversation at all — it can never
buy an agent turn.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

import httpx
import pytest
from _bridge_support import BridgeHarness, cancel_and_join, request_mentions, script_reply

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import SESSION_COOKIE, WebChatClient, mint_unregistered_token, transcript_keys

# The scripted-LLM turns are the 'llm' mock leg; a real LLM breaks the scripting. The web
# channel itself has no vendor and so no real/mock split — it is always real.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted-LLM turns are the 'llm' mock leg; the real leg runs on the creds host (PLAN_2 §F)",
)

# Room for the JSON envelope around a message text, so a body sized off the byte cap
# below still arrives under it and is refused on the TEXT rule rather than the byte one.
_ENVELOPE_BYTES = 1024


def _body_cap(bridge: BridgeHarness) -> int:
    """The POST body byte cap this stack CONFIGURES, read back off its own
    ``CHANNEL_WEB_MAX_BODY_BYTES`` env — so re-tuning the profile moves the leg with it
    instead of turning it red against a number restated here."""
    return int(bridge.stack.config.env["CHANNEL_WEB_MAX_BODY_BYTES"])


def _outbound_message(text: str) -> Callable[[str, dict], bool]:
    """Predicate for the agent's answer arriving on the visitor's live stream."""
    return lambda event, data: event == "chat.message" and data["direction"] == "out" and text in data["text"]


async def _create_web_route(bridge: BridgeHarness, uniq: Callable[[str], str], tag: str) -> str:
    """Create a web conversation route on its own identity; returns that identity."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    execution_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=execution_key,
        channel="web",
        our_identity=identity,
    )
    return identity


async def _open_visitor(bridge: BridgeHarness, identity: str) -> tuple[WebChatClient, httpx.Response]:
    """Open the chat page as a first-time visitor, returning the door client bound to the
    session the page minted and registered, plus the page itself."""
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(base_url, identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text
    return web, page


async def _open_web_route(bridge: BridgeHarness, uniq: Callable[[str], str], tag: str) -> WebChatClient:
    """Create a web conversation route and open its chat page as a first-time visitor,
    checking the shell the bundle boots into on the way."""
    identity = await _create_web_route(bridge, uniq, tag)
    web, page = await _open_visitor(bridge, identity)
    # The shell the bundle boots into, stamped with the route this page talks to.
    assert f'data-identity="{identity}"' in page.text
    # The module the shell links resolves on the asset door — a page whose entry 404s
    # would still serve a 200 shell and boot to nothing.
    entry = re.search(r'src="/api/channels/web/assets/([^"]+)"', page.text)
    assert entry is not None, page.text[:1000]
    assert (await web.asset(entry.group(1))).status_code == 200
    return web


async def test_web_visitor_message_round_trips_and_the_stream_replays_it(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    web = await _open_web_route(bridge, uniq, "l12")

    marker = uniq("remember")
    answer1 = uniq("l12-ans1")
    answer2 = uniq("l12-ans2")
    script_reply(bridge.llm_stub, f"Noted: {answer1}", f"Recalling {answer2}")

    # The page keeps its stream open across a send, exactly as the real one does. The open
    # and the send are started together and the reader judges the backlog as well as the
    # tail, so the answer is caught whichever side of the open it lands on.
    tail = asyncio.create_task(web.frames(until=_outbound_message(answer1), deadline=40.0))
    try:
        sent = await web.send(f"my secret is {marker}")
        assert sent.status_code == 200, sent.text
        assert sent.json()["data"]["message_id"]
        await asyncio.wait_for(tail, timeout=45.0)
    finally:
        await cancel_and_join(tail)

    # Fire 2 on the same visitor session: the turn answers from the restored history.
    tail2 = asyncio.create_task(web.frames(until=_outbound_message(answer2), deadline=40.0))
    try:
        second = await web.send("what was my secret?")
        assert second.status_code == 200, second.text
        await asyncio.wait_for(tail2, timeout=45.0)
    finally:
        await cancel_and_join(tail2)

    assert request_mentions(bridge.llm_stub, 1, marker), (
        "the second web turn did not see the first turn's history; checkpoint continuity broke"
    )

    # Reconnect: a fresh stream replays the whole exchange, in order, from the backlog.
    replayed = await web.frames()
    exchange = [(data["direction"], data["text"]) for event, data in replayed if event == "chat.message"]
    assert [direction for direction, _text in exchange] == ["in", "out", "in", "out"]
    assert marker in exchange[0][1]
    assert answer1 in exchange[1][1]
    assert answer2 in exchange[3][1]


async def test_ask_user_lands_in_the_live_web_conversation(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    web = await _open_web_route(bridge, uniq, "l12ask")
    # A SECOND registered visitor on the same route — a real session owning another
    # conversation, which is what separates "not yours" from "no session at all".
    other, _other_page = await _open_visitor(bridge, web.identity)
    assert other.visitor_id != web.visitor_id

    question = uniq("l12-ask-q")
    ask_answer = uniq("l12-ask-a")

    async def ask() -> object:
        # A web ask names its target as the visitor pair; there is no operator default.
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            result = await mcp.call_tool(
                "ask_user",
                {"question": question, "channel": "web", "recipient": web.recipient},
            )
        return result.data

    ask_task = asyncio.create_task(ask())
    try:
        delivered = await web.frames(
            until=lambda event, data: event == "chat.question" and data["question"] == question,
            deadline=30.0,
        )
        asked = next(data for event, data in delivered if event == "chat.question")
        # A text question answers by interaction id through this plugin's own door, so the
        # frame never carries the callback ticket.
        assert "callback_url" not in asked, asked

        # A well-formed token nobody registered is no session at all (401), and another
        # visitor's REGISTERED session cannot answer this conversation's question (404,
        # never "exists, but not yours"). The owning session then does.
        invented = await web.answer(
            asked["interaction_id"], ask_answer, cookies={SESSION_COOKIE: mint_unregistered_token()}
        )
        assert invented.status_code == 401, invented.text
        assert invented.json()["code"] == "session_missing"
        foreign = await web.answer(asked["interaction_id"], ask_answer, cookies=other.cookies)
        assert foreign.status_code == 404, foreign.text
        accepted = await web.answer(asked["interaction_id"], ask_answer)
        assert accepted.status_code == 200, accepted.text

        resolved = await asyncio.wait_for(ask_task, timeout=20.0)
    finally:
        await cancel_and_join(ask_task)

    assert resolved == ask_answer
    # The settled question is in the visitor's own replay, so the page can render it as
    # answered after a reconnect.
    replayed = await web.frames()
    assert any(event == "chat.answered" and data["answer"] == ask_answer for event, data in replayed)


async def test_an_invented_cookie_cannot_open_a_conversation(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """The spend fence: only the chat page mints AND registers a session.

    A caller that invents a well-formed cookie value passes the format gate and still
    resolves to no visitor, so the message door refuses before ``accept`` is reached — no
    conversation is opened, no turn is bought, and no fresh address appears that would
    dodge the bridge's per-address turn cap. The door mints nothing of its own either: a
    session comes from the page or from nowhere.
    """
    identity = await _create_web_route(bridge, uniq, "l12spend")
    store_url = bridge.stack.resources.redis_url
    assert transcript_keys(store_url, identity) == set()

    invented = mint_unregistered_token()
    speculative = WebChatClient(
        base_url=f"http://{bridge.stack.host}:{bridge.stack.port_b}",
        identity=identity,
        token=invented,
        # No registration exists, so there is no server-side address; the census below is
        # what proves the door created none.
        visitor_id="",
    )

    refused = await speculative.send(uniq("l12-spend"))
    assert refused.status_code == 401, refused.text
    assert refused.json()["code"] == "session_missing"
    # The refusal hands back no session either — the message door never mints one.
    assert SESSION_COOKIE not in refused.cookies
    # The read door is shut the same way, so the invented token cannot even watch.
    async with speculative.hold_stream() as stream:
        assert stream.status_code == 401, stream.text
        assert stream.json()["code"] == "session_missing"
    # Nothing was written under this route: no address was conjured for the invented token.
    assert transcript_keys(store_url, identity) == set()

    # Contrast: the page-minted, page-registered session on the same route does open one.
    visitor, _visitor_page = await _open_visitor(bridge, identity)
    script_reply(bridge.llm_stub, uniq("l12-spend-ans"))
    accepted = await visitor.send(uniq("l12-spend-real"))
    assert accepted.status_code == 200, accepted.text
    assert transcript_keys(store_url, identity) == {visitor.transcript_key}


async def test_message_door_refuses_an_over_cap_body_and_an_unusable_body(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """The message door's edge refusals, in the order the door applies them: the body is
    read under a byte cap first (413, counted as bytes actually read), then validated
    (422) — so an over-long text is a precise 422 and never an opaque 413."""
    identity = await _create_web_route(bridge, uniq, "l12caps")
    web, _page = await _open_visitor(bridge, identity)

    over_cap = await web.send("x" * (_body_cap(bridge) + 1024))
    assert over_cap.status_code == 413, over_cap.text
    assert "too large" in over_cap.json()["error"]

    # The longest text the byte cap can carry. The door's text cap sits UNDER its body
    # cap, so this is over-long by construction and the leg names no cap of its own — the
    # text cap is a plugin module constant behind no setting, and restating its value here
    # would turn this red the day the plugin re-tunes it.
    over_long = await web.send("y" * (_body_cap(bridge) - _ENVELOPE_BYTES))
    assert over_long.status_code == 422, over_long.text

    # A ':' separates a composite recipient and qualifies the transcript key, so an
    # identity carrying one is not a usable route name; nor is a blank one.
    assert (await web.send(uniq("l12-caps"), identity=f"{identity}:extra")).status_code == 422
    assert (await web.send(uniq("l12-caps"), identity="   ")).status_code == 422

    # Every refusal happened at the edge: nothing reached the bridge, so the visitor's
    # transcript is still empty.
    assert await web.frames() == []
