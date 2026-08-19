"""L14 — the thread-listing filters and the message text search over a real bridge
conversation.

Two turns on one routed pair leave a thread behind; the operator narrows the listing with
``?status=``/``?address=``, narrows a transcript with ``?q=``, and searches the whole route
with ``GET /api/conversations/{route}/messages/search?q=``. Each filter is a bounded
post-scan, so every envelope carries a ``truncated`` flag; on this small conversation it is
always ``false``, proving the flag is published and off, never a silent cut.

Driven over twilio: the filters are the SKELETON's, so they lean on the most-proven inbound
path rather than any one channel plugin.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    wait_twilio_send,
)

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm") or HarnessSettings().is_real("twilio"),
    reason="scripted-LLM + FakeTwilio are the 'llm'/'twilio' mock leg; real on the creds host",
)


def _transcript_path(route_name: str, thread_id: str, **query: object) -> str:
    q = urlencode({"thread_id": thread_id, **{key: str(value) for key, value in query.items()}})
    return f"/api/conversations/{route_name}/transcript?{q}"


async def _channel_route(bridge: BridgeHarness, uniq: Callable[[str], str]) -> tuple[str, str]:
    route_name = uniq("l14-route").replace("_", "-")
    execution_key = uniq("l14-exec")
    identity = f"+1555{secrets.randbelow(10**7):07d}"
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name, agent="tools_agent", execution_key=execution_key, channel="twilio", our_identity=identity
    )
    return route_name, identity


async def _two_exchanges(bridge: BridgeHarness, uniq: Callable[[str], str], identity: str) -> list[tuple[str, str]]:
    exchanges = [(uniq("l14-in1"), uniq("l14-out1")), (uniq("l14-in2"), uniq("l14-out2"))]
    script_reply(bridge.llm_stub, *[answer for _text, answer in exchanges])
    port = bridge.stack.port_b
    for text, answer in exchanges:
        inbound = bridge.twilio_inbound(our_identity=identity, client=BRIDGE_TWILIO_CLIENT, text=text, port=port)
        assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204
        await wait_twilio_send(bridge.fake_twilio, answer)
    return exchanges


# The full delivery-status vocabulary the door validates against — the same enum the store
# indexes by. A value outside it is a loud 400.
_DELIVERY_STATUSES = ("accepted", "pending_delivery", "provisional", "delivered", "failed", "shed", "silent")


async def test_thread_filters_narrow_the_listing_and_the_search(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    route_name, identity = await _channel_route(bridge, uniq)
    exchanges = await _two_exchanges(bridge, uniq, identity)

    (thread,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
    thread_id = thread["thread_id"]
    status = thread["last_delivery_status"]

    # (a) status filter: the thread's own status matches; a DIFFERENT valid status excludes it.
    # Both envelopes publish ``truncated`` and, on this tiny route, it is off.
    matched = await bridge.api().get(f"/api/conversations/{route_name}/threads?status={status}")
    assert [item["thread_id"] for item in matched["items"]] == [thread_id]
    assert matched["truncated"] is False
    other_status = next(s for s in _DELIVERY_STATUSES if s != status)
    missed = await bridge.api().get(f"/api/conversations/{route_name}/threads?status={other_status}")
    assert missed["items"] == []
    assert missed["truncated"] is False

    # (b) an unknown status is a loud 400 at the edge, never a silently ignored filter.
    await bridge.api().get(f"/api/conversations/{route_name}/threads?status=nope", expect=400)

    # (c) address filter: a substring of the client address keeps the thread; a substring that
    # is nowhere in it excludes it.
    hit = await bridge.api().get(f"/api/conversations/{route_name}/threads?address={BRIDGE_TWILIO_CLIENT[-4:]}")
    assert [item["thread_id"] for item in hit["items"]] == [thread_id]
    miss = await bridge.api().get(f"/api/conversations/{route_name}/threads?address=zzz-nowhere")
    assert miss["items"] == []

    # (d) transcript ``q``: a word from the FIRST inbound keeps only that record; a word in no
    # record is an empty page (never a 404, because the thread itself exists).
    needle = exchanges[0][0]
    filtered = await bridge.api().get(_transcript_path(route_name, thread_id, q=needle))
    assert [item["inbound_text"] for item in filtered["items"]] == [exchanges[0][0]]
    assert filtered["truncated"] is False
    empty = await bridge.api().get(_transcript_path(route_name, thread_id, q="zzz-nowhere"))
    assert empty["items"] == []

    # (e) the route-scoped message search spans the thread's records; a matching word finds the
    # record whichever thread it lives on, and the envelope carries ``truncated``.
    found = await bridge.api().get(f"/api/conversations/{route_name}/messages/search?{urlencode({'q': needle})}")
    assert needle in [item["inbound_text"] for item in found["items"]]
    assert found["truncated"] is False
    # The search is admin-only and ``q`` is required: a bare call is a loud 400.
    await bridge.api().get(f"/api/conversations/{route_name}/messages/search", expect=400)
