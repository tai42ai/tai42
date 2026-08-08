"""L13 — the conversation thread read doors over a real bridge conversation.

Two turns on one routed pair leave a thread behind; the operator reads it back through
``GET /api/conversations/{route}/threads`` and
``GET /api/conversations/{route}/transcript?thread_id=``. The transcript must carry both
halves of each exchange — the visitor's ``inbound_text`` and the agent's ``answer`` — in
the direction ``?order=`` asks for, with the paging window the doors advertise. The thread
id rides the QUERY because an api-door id carries a percent-encoded principal that no path
spelling round-trips.

The paging/order and refusal arms are driven over twilio deliberately: the read doors are
the SKELETON's, so they lean on the most-proven inbound path rather than on any one channel
plugin. A channel thread names no caller principal, so no non-admin can own one — that arm
proves both refusals, and that they are told apart: the thread LISTING is an outright 403,
while the transcript door answers ONE uniform 404 for every thread the caller cannot read,
so it can never be used to probe whether an address has talked to a route.

Ownership itself only exists on the API door, so it is proven there: an api-door thread
names its owner inside its own id, as the caller's principal percent-encoded with
``safe=''``. Producer (the turn that composes the address) and consumer (the read that
authorizes from it) must spell that encoding identically, and a drift between them would
hide behind the very 404 that is indistinguishable from "no such thread". The owner arm
therefore drives a principal that genuinely needs encoding and asserts the caller reads
their own records back.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from urllib.parse import quote, urlencode

import pytest
from _bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    wait_twilio_send,
)

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings

# Every leg scripts the LLM stub and asserts the scripted answer back, so the whole module
# is the 'llm' mock leg; the twilio-driven ones additionally need FakeTwilio's signed
# inbound. The real legs run on the dedicated creds host (PLAN_2 §F), not in CI.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted-LLM is the 'llm' mock leg; the real leg runs on the creds host (PLAN_2 §F)",
)
MOCK_TWILIO_ONLY = pytest.mark.skipif(
    HarnessSettings().is_real("twilio"),
    reason="FakeTwilio inbound is the 'twilio' mock leg; the real leg runs on the creds host (PLAN_2 §F)",
)

# An https callback URL with nothing behind it. Every api-door turn here finishes inside its
# sync wait, which suppresses the callback, so it is never dialled.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _transcript_path(route_name: str, thread_id: str, **window: object) -> str:
    """The transcript door's URL: the thread id is a QUERY value, encoded exactly once by
    the query parser whatever it holds."""
    query = urlencode({"thread_id": thread_id, **{key: str(value) for key, value in window.items()}})
    return f"/api/conversations/{route_name}/transcript?{query}"


async def _channel_route(bridge: BridgeHarness, uniq: Callable[[str], str]) -> tuple[str, str]:
    """A fresh twilio-door route bound to its own execution key; returns
    ``(route name, our_identity)``.

    Each spec gets its OWN deployment number: one ``(channel, our_identity)`` pair routes to
    exactly one route, so specs sharing this module-scoped stack must not share a number."""
    route_name = uniq("l13-route").replace("_", "-")
    execution_key = uniq("l13-exec")
    identity = f"+1555{secrets.randbelow(10**7):07d}"
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=execution_key,
        channel="twilio",
        our_identity=identity,
    )
    return route_name, identity


async def _api_route(bridge: BridgeHarness, uniq: Callable[[str], str]) -> str:
    """A fresh api-door route bound to its own execution key; returns the route name.

    The api door is the ONLY door whose threads have an owner: it composes the thread's
    address out of the authenticated caller's principal, so a caller can be shown their own
    transcript without being an administrator."""
    route_name = uniq("l13-api").replace("_", "-")
    execution_key = uniq("l13-api-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_api_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=execution_key,
        callback_url=_UNREACHABLE_CALLBACK,
    )
    return route_name


async def _two_exchanges(bridge: BridgeHarness, uniq: Callable[[str], str], identity: str) -> list[tuple[str, str]]:
    """Route two inbound messages through the bridge on one pair; return the
    ``(inbound text, answer)`` exchanges in the order they were sent."""
    exchanges = [(uniq("l13-in1"), uniq("l13-out1")), (uniq("l13-in2"), uniq("l13-out2"))]
    script_reply(bridge.llm_stub, *[answer for _text, answer in exchanges])
    port = bridge.stack.port_b
    for text, answer in exchanges:
        inbound = bridge.twilio_inbound(our_identity=identity, client=BRIDGE_TWILIO_CLIENT, text=text, port=port)
        response = await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)
        assert response.status_code == 204, response.text
        # Wait for the answer to leave before firing the next, so the transcript order is
        # the send order and not a race between two in-flight turns.
        await wait_twilio_send(bridge.fake_twilio, answer)
    return exchanges


@MOCK_TWILIO_ONLY
async def test_threads_and_transcript_read_back_a_bridge_conversation(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    route_name, identity = await _channel_route(bridge, uniq)
    exchanges = await _two_exchanges(bridge, uniq, identity)

    listing = await bridge.api().get(f"/api/conversations/{route_name}/threads")
    assert listing["total"] == 1
    assert listing["page"] == 1
    assert listing["page_size"] == 50
    assert listing["next_page"] is None
    (thread,) = listing["items"]
    assert thread["client_address"] == BRIDGE_TWILIO_CLIENT
    assert thread["message_count"] == len(exchanges)
    assert thread["last_activity_at"] > 0
    assert thread["last_delivery_status"]

    thread_id = thread["thread_id"]
    transcript = await bridge.api().get(_transcript_path(route_name, thread_id))
    assert transcript["total"] == len(exchanges)
    assert transcript["page"] == 1
    assert transcript["page_size"] == 50
    assert transcript["next_page"] is None
    # Oldest first by default, both halves of each exchange, in the order they were sent —
    # and the answer echoes the direction it was read in.
    assert transcript["order"] == "asc"
    assert [(item["inbound_text"], item["answer"]) for item in transcript["items"]] == exchanges
    assert all(item["thread_id"] == thread_id for item in transcript["items"])

    # ``order=desc`` reads the same thread newest first — the direction a live tail wants,
    # because page 1 then always holds the latest messages (the Studio monitor's read).
    newest = await bridge.api().get(_transcript_path(route_name, thread_id, order="desc"))
    assert newest["order"] == "desc"
    assert newest["total"] == len(exchanges)
    assert [(item["inbound_text"], item["answer"]) for item in newest["items"]] == exchanges[::-1]
    # The window applies to that direction from ITS own end: page 1 of desc is the newest
    # page and never the oldest.
    newest_page = await bridge.api().get(_transcript_path(route_name, thread_id, order="desc", page=1, pageSize=1))
    assert [item["inbound_text"] for item in newest_page["items"]] == [exchanges[-1][0]]
    assert newest_page["next_page"] == 2

    # A page that does not exhaust the thread advertises the next one; a page size above
    # the server cap is capped rather than refused.
    first = await bridge.api().get(_transcript_path(route_name, thread_id, page=1, pageSize=1))
    assert first["total"] == len(exchanges)
    assert first["page_size"] == 1
    assert first["next_page"] == 2
    assert [item["inbound_text"] for item in first["items"]] == [exchanges[0][0]]
    capped = await bridge.api().get(f"/api/conversations/{route_name}/threads?pageSize=5000")
    assert capped["page_size"] == 200


@MOCK_TWILIO_ONLY
async def test_unknown_route_and_thread_are_uniform_404s(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    route_name, _identity = await _channel_route(bridge, uniq)
    missing_route = uniq("l13-nosuch").replace("_", "-")

    await bridge.api().get(f"/api/conversations/{missing_route}/threads", expect=404)
    await bridge.api().get(_transcript_path(missing_route, "bridge:nope"), expect=404)
    # A real route, a thread it never held: the same uniform 404, so a probe cannot tell a
    # never-seen thread from one living under another route.
    await bridge.api().get(_transcript_path(route_name, "bridge:nope"), expect=404)
    # A malformed window is a loud 400 at the edge, never a silently clamped read.
    await bridge.api().get(f"/api/conversations/{route_name}/threads?page=0", expect=400)
    await bridge.api().get(f"/api/conversations/{route_name}/threads?pageSize=nine", expect=400)
    # The transcript door's own required/typed parameters, each a loud 400: no thread id at
    # all, a blank one, and a direction that is neither asc nor desc.
    await bridge.api().get(f"/api/conversations/{route_name}/transcript", expect=400)
    await bridge.api().get(_transcript_path(route_name, "   "), expect=400)
    await bridge.api().get(_transcript_path(route_name, "bridge:nope", order="sideways"), expect=400)


@MOCK_TWILIO_ONLY
async def test_channel_threads_are_admin_only(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    route_name, identity = await _channel_route(bridge, uniq)
    await _two_exchanges(bridge, uniq, identity)
    thread_id = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"][0]["thread_id"]

    # A scoped key reaches the doors (the route table maps them to e2e-all) but is not an
    # administrator. The two doors refuse it in the two different shapes their contracts
    # promise: the listing spans every caller on the route, so it is refused outright with
    # a 403; a channel thread names no caller principal, so no caller can own one, and the
    # transcript door answers the SAME 404 it gives any unreadable thread.
    scoped = await bridge.mint_key(user_id=uniq("l13-scoped"), scopes=["e2e-all"])
    reader = bridge.api(token=scoped)
    await reader.get(f"/api/conversations/{route_name}/threads", expect=403)
    # The refusal for a thread that DOES exist and the one for a thread that never did
    # differ only in the id the caller themselves supplied: the door leaks no oracle for
    # whether this address has ever talked to the route.
    real = await reader.request_raw("GET", _transcript_path(route_name, thread_id))
    absent = await reader.request_raw("GET", _transcript_path(route_name, "bridge:nope"))
    assert real.status_code == absent.status_code == 404
    assert real.json() == {"error": f"conversation thread not found: {thread_id!r}"}, real.text
    assert absent.json() == {"error": "conversation thread not found: 'bridge:nope'"}, absent.text

    # The refusals are authorization, not absence: the admin still reads both.
    assert (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["total"] == 1
    assert (await bridge.api().get(_transcript_path(route_name, thread_id)))["total"] == 2


async def test_api_door_owner_reads_their_own_transcript(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    """A NON-admin api caller reads back the thread its own principal keys.

    The door authorizes from the thread's identity alone, and an api-door thread id ends in
    ``{principal percent-encoded with safe=''}/{end-user id}``. So the turn that WRITES the
    address and the read that AUTHORIZES from it must spell the encoding the same way, and
    any drift would land as the door's uniform 404 — indistinguishable from "no such
    thread". This caller's principal carries the ``:`` an OIDC subject does, so it is only
    readable if the two spellings agree on percent-encoding."""
    route_name = await _api_route(bridge, uniq)
    principal = f"oidc:google:{uniq('l13-owner')}"
    encoded = quote(principal, safe="")
    assert encoded != principal, "this leg only bites while the principal needs percent-encoding"
    caller = bridge.api(token=await bridge.mint_key(user_id=principal, scopes=["e2e-all"]))
    end_user = uniq("l13-enduser")

    exchanges = [(uniq("l13-api-in1"), uniq("l13-api-out1")), (uniq("l13-api-in2"), uniq("l13-api-out2"))]
    script_reply(bridge.llm_stub, *[answer for _text, answer in exchanges])
    threads = set()
    for text, answer in exchanges:
        # A sync wait carries the answer inline, so the turn is finished — and the record
        # written — by the time the door answers; no polling stands between the two turns.
        accepted = await caller.post(
            f"/api/conversations/{route_name}/messages",
            json={"external_user_id": end_user, "text": text, "wait_seconds": 20},
            expect=200,
        )
        assert accepted["answer"]["status"] == "answered"
        assert accepted["answer"]["answer"] == answer
        threads.add(accepted["thread_id"])
    # Both turns of one (caller, end user) pair belong to ONE thread, and its id names the
    # caller's principal in the encoded form the read has to reproduce.
    (thread_id,) = threads
    assert thread_id.endswith(f":{encoded}/{end_user}")

    # The proof: the owner — not an administrator — is served their own whole transcript.
    transcript = await caller.get(_transcript_path(route_name, thread_id))
    assert transcript["total"] == len(exchanges)
    assert [(item["inbound_text"], item["answer"]) for item in transcript["items"]] == exchanges
    assert all(item["thread_id"] == thread_id for item in transcript["items"])
    # Served as the CALLER projection, so this is the non-admin path and not an accidental
    # admin one: every record names this principal and withholds the route key's run detail.
    assert all(item["caller_principal"] == principal for item in transcript["items"])
    assert all("error" not in item for item in transcript["items"])

    # Ownership is per principal, never "any authenticated non-admin": a second scoped key
    # gets the one uniform 404 for the very thread the owner just read.
    stranger = bridge.api(token=await bridge.mint_key(user_id=uniq("l13-stranger"), scopes=["e2e-all"]))
    await stranger.get(_transcript_path(route_name, thread_id), expect=404)
    # And owning a thread buys no listing: that door spans every caller on the route, so it
    # stays an outright 403 on the api door too.
    await caller.get(f"/api/conversations/{route_name}/threads", expect=403)
    (listed,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
    assert listed["thread_id"] == thread_id
    assert listed["client_address"] == f"{encoded}/{end_user}"
    assert listed["message_count"] == len(exchanges)
