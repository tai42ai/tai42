"""A corrupt conversation record surfaces as ``unreadable`` and the sweep fails it terminally.

A record row whose stored content blob cannot be parsed is the case this suite drives on the
live stack. It must never shorten a listing silently: every admin listing that enumerates the
row reports it in an ``unreadable`` count while omitting only that member, and the periodic
delivery sweep moves an unreadable ``pending_delivery``/``provisional`` row to the terminal
``failed`` state so it stops being re-enumerated every pass and lands on the admin failed
listing.

Two real bridge exchanges leave two records in one thread; the row of one is corrupted through
the conversations store's own hash (a malformed content blob) and indexed ``pending_delivery``,
the way the skeleton's store unit tests corrupt a row. The admin transcript then reports
``unreadable == 1`` with only the corrupt member omitted; after ``e2e_sweep_stalled_deliveries``
drives one sweep pass inside the worker, the row is terminal ``failed`` (its status flipped, it
left the pending index for the failed one) and the admin failed listing carries it as an
unreadable member.

The store keys are built from the stack's ``CONVERSATIONS_PREFIX`` (``<bus namespace>:conversations``)
and the store's own key shapes; the corruption is written over the stack's shared Redis exactly as
``_stack_redis_get`` peeks it elsewhere in this suite.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from urllib.parse import urlencode

import pytest
import redis as redis_lib

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.stack import TaiStack

from ._bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    wait_twilio_send,
)

# Every leg scripts the LLM stub and asserts the scripted answer back, so the whole module is
# the 'llm' mock leg; the twilio-driven inbound additionally needs FakeTwilio's signed inbound.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted-LLM is the 'llm' mock leg; the real leg runs on the creds host",
)
MOCK_TWILIO_ONLY = pytest.mark.skipif(
    HarnessSettings().is_real("twilio"),
    reason="FakeTwilio inbound is the 'twilio' mock leg; the real leg runs on the creds host",
)

# A content blob that is not valid JSON: the store's ``_from_hash`` raises decoding it, which is
# the unreadable-row condition every listing counts and the sweep fails terminally.
_MALFORMED_BLOB = "{not json"


def _prefix(bridge: BridgeHarness) -> str:
    """The conversations store key prefix this stack runs under (``CONVERSATIONS_PREFIX``)."""
    return f"{bridge.stack.resources.bus_namespace}:conversations"


def _record_key(bridge: BridgeHarness, message_id: str) -> str:
    return f"{_prefix(bridge)}:record:{message_id}"


def _status_index_key(bridge: BridgeHarness, delivery_status: str) -> str:
    return f"{_prefix(bridge)}:status:{delivery_status}"


def _stack_redis(bridge: BridgeHarness) -> redis_lib.Redis:
    return redis_lib.Redis.from_url(bridge.stack.resources.redis_url, decode_responses=True)


def _transcript_path(route_name: str, thread_id: str) -> str:
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"


async def _twilio_route(bridge: BridgeHarness, uniq: Callable[[str], str]) -> tuple[str, str]:
    """A fresh twilio-door route bound to its own execution key; returns ``(route, our_identity)``."""
    route_name = uniq("a2-route").replace("_", "-")
    execution_key = uniq("a2-exec")
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


async def _two_records(bridge: BridgeHarness, uniq: Callable[[str], str], identity: str) -> int:
    """Route two inbound messages through the bridge on one pair, waiting for each answer to
    leave before the next; returns the exchange count."""
    exchanges = [(uniq("a2-in1"), uniq("a2-out1")), (uniq("a2-in2"), uniq("a2-out2"))]
    script_reply(bridge.llm_stub, *[answer for _text, answer in exchanges])
    port = bridge.stack.port_b
    for text, answer in exchanges:
        inbound = bridge.twilio_inbound(our_identity=identity, client=BRIDGE_TWILIO_CLIENT, text=text, port=port)
        response = await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)
        assert response.status_code == 204, response.text
        await wait_twilio_send(bridge.fake_twilio, answer)
    return len(exchanges)


@MOCK_TWILIO_ONLY
async def test_corrupt_record_counts_unreadable_and_sweep_moves_it_to_failed(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    stack: TaiStack = bridge.stack
    route_name, identity = await _twilio_route(bridge, uniq)
    count = await _two_records(bridge, uniq, identity)

    # The two records land in one thread; read it back admin-side to learn the ids, and confirm
    # the all-clean transcript reports no unreadable member.
    (thread,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
    thread_id = thread["thread_id"]
    clean = await bridge.api().get(_transcript_path(route_name, thread_id))
    assert clean["unreadable"] == 0, clean
    message_ids = [item["message_id"] for item in clean["items"]]
    assert len(message_ids) == count == 2, message_ids
    corrupt_id, readable_id = message_ids[0], message_ids[1]

    # Corrupt one record's stored blob and index it pending, the way the store unit tests do —
    # a live record whose row the sweep can no longer parse.
    client = _stack_redis(bridge)
    try:
        client.hset(
            _record_key(bridge, corrupt_id),
            mapping={"data": _MALFORMED_BLOB, "delivery_status": "pending_delivery"},
        )
        client.zadd(_status_index_key(bridge, "pending_delivery"), {corrupt_id: float("inf")})
    finally:
        client.close()

    # (a) The admin transcript now counts the corrupt member as unreadable, omitting ONLY it.
    corrupted = await bridge.api().get(_transcript_path(route_name, thread_id))
    assert corrupted["unreadable"] == 1, corrupted
    assert [item["message_id"] for item in corrupted["items"]] == [readable_id], corrupted

    # (b) Drive one delivery sweep pass inside the worker; the unreadable pending row is failed.
    async with stack.mcp(auth=bridge.root_token) as mcp:
        swept = await mcp.call_tool("e2e_sweep_stalled_deliveries", {}, retry_on_reloading=True)
    assert swept.data["swept"] is True, swept.data

    # The row is terminal ``failed``: its status hash flipped and it moved out of the pending
    # index into the failed one.
    client = _stack_redis(bridge)
    try:
        assert client.hget(_record_key(bridge, corrupt_id), "delivery_status") == "failed"
        assert client.zscore(_status_index_key(bridge, "failed"), corrupt_id) is not None
        assert client.zscore(_status_index_key(bridge, "pending_delivery"), corrupt_id) is None
    finally:
        client.close()

    # The admin failed listing carries the terminal-failed row as an unreadable member (its blob
    # stays unparseable), never a silently shorter list.
    failed = await bridge.list_failed()
    assert failed["unreadable"] >= 1, failed
    assert corrupt_id not in [item.get("message_id") for item in failed["items"]], failed
