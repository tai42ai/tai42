"""Receipt-race: a delivery receipt that lands while the send record is still pending_delivery.

The platform publishes the outbound reverse index per chunk (so a receipt can resolve to the
record) BEFORE the send loop reaches ``mark_provisional``. A receipt arriving in that window
resolves to a record still in ``pending_delivery``; the skeleton seam PARKS it (stages the
terminal target on ``pending_receipt`` and returns without raising) so the door acks, and the
completing ``mark_provisional`` applies the parked receipt straight to the terminal. This drives
that exact ordering end to end.

The ordering is made deterministic by a mid-send barrier on the twilio stub: the answer is long
enough to split into more than one SMS chunk, and the stub holds the SECOND chunk's send open
while the test posts the vendor status callback for the FIRST chunk's already-indexed
``MessageSid``. That guarantees the callback lands while the record is still ``pending_delivery``
(the first chunk is indexed, ``mark_provisional`` has not run). While the send is held, the
record is read back through the admin transcript door to prove the receipt is parked
(``delivery_status`` still ``pending_delivery``, ``pending_receipt`` set); once the send
completes the record is terminal ``delivered`` with the answer intact and every chunk sent
exactly once.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_FROM
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import (
    TWILIO_INBOUND_PATH,
    TWILIO_STATUS_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    wait_channel_send_count,
)

# The scripted-LLM + FakeTwilio flow is the mock leg for the 'twilio' and 'llm' seams; either
# selection real breaks the stub scripting, so the module steps aside. The reproduction leans on
# the twilio stub's mid-send barrier: without a controllable hold on the send the sub-millisecond
# pending_delivery window cannot be hit deterministically.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("llm"),
    reason="FakeTwilio + scripted-LLM is the 'twilio'/'llm' mock leg; real legs on the creds host",
)

# Long enough to split into more than one twilio SMS chunk (max_message_chars['twilio'] == 1600),
# with the marker in every chunk so a send count matches on any chunk and the transcript answer
# is found by substring.
_LONG_ANSWER_TOKENS = 500


def _long_answer(marker: str) -> str:
    return " ".join(f"{marker}-{index:04d}" for index in range(_LONG_ANSWER_TOKENS))


def _transcript_path(route_name: str, thread_id: str) -> str:
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id})}"


async def _answer_record(bridge: BridgeHarness, route_name: str, marker: str) -> dict | None:
    """The admin transcript item for the answer carrying ``marker`` under ``route_name``, or
    ``None`` while none is written yet. Admin projection, so it carries ``delivery_status`` and
    ``pending_receipt``."""
    listing = await bridge.api().get(f"/api/conversations/{route_name}/threads")
    for thread in listing["items"]:
        transcript = await bridge.api().get(_transcript_path(route_name, thread["thread_id"]))
        for item in transcript["items"]:
            if marker in (item.get("answer") or ""):
                return item
    return None


async def test_receipt_before_provisional_is_parked_and_applied(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = BRIDGE_TWILIO_FROM
    client = BRIDGE_TWILIO_CLIENT
    exec_key = uniq("rr-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("rr-route").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_name, agent="tools_agent", execution_key=exec_key, channel="twilio", our_identity=identity
    )
    port = bridge.stack.port_b

    marker = uniq("rr-ans")
    answer = _long_answer(marker)
    script_reply(bridge.llm_stub, answer)

    # The barrier fires after each recorded send; on the second chunk it posts the delivered
    # status for the first chunk's already-indexed sid (the record is still pending_delivery),
    # then snapshots the record through the admin transcript to prove the park. Failures are
    # captured and re-raised in the test so a barrier fault never hides behind a delivery 5xx.
    parked: list[dict] = []
    ack_codes: list[int] = []
    errors: list[BaseException] = []

    async def barrier() -> None:
        # Act once, when the answer's SECOND chunk has just been recorded: the first chunk's sid
        # is already published in the reverse index, the send has not reached mark_provisional, so
        # the record is still pending_delivery. Holding this chunk's send open keeps it there
        # while the receipt for the first chunk is posted and the parked record is read back.
        chunks = bridge.fake_twilio.sends_matching(marker)
        if len(chunks) != 2 or parked or errors:
            return
        try:
            status = bridge.twilio_status(message_sid=chunks[0]["sid"], status="delivered", port=port)
            resp = await post_inbound(bridge.stack, TWILIO_STATUS_PATH, status, port=port)
            ack_codes.append(resp.status_code)
            snapshot = await wait_for_async(
                lambda: _answer_record(bridge, route_name, marker),
                deadline=10.0,
                message="answer record never appeared in the transcript while the send was held",
            )
            parked.append(snapshot)
        except BaseException as exc:
            errors.append(exc)

    bridge.fake_twilio.send_barrier = barrier
    try:
        inbound = bridge.twilio_inbound(our_identity=identity, client=client, text="hello", port=port)
        assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204

        # Wait for the held send to capture the parked snapshot (or report a barrier fault). The
        # barrier runs in the stub's own loop, so the test must not race past it.
        async def _barrier_done() -> bool:
            return bool(parked or errors)

        await wait_for_async(
            _barrier_done, deadline=45.0, message="the mid-send barrier never posted the early receipt"
        )
    finally:
        bridge.fake_twilio.send_barrier = None

    if errors:
        raise errors[0]

    assert len(bridge.fake_twilio.sends_matching(marker)) >= 2, (
        "the answer must split into at least two chunks to hold the race window open"
    )

    # The receipt door acked the early receipt (no 5xx / redelivery storm).
    assert ack_codes == [204], f"expected the held-send receipt to ack 204, saw {ack_codes!r}"

    # The receipt was parked while the record was still pending_delivery: the admin projection
    # carries pending_receipt staged to the terminal target.
    (snapshot,) = parked
    assert snapshot["delivery_status"] == "pending_delivery", snapshot
    assert snapshot["pending_receipt"] == "delivered", snapshot
    message_id = snapshot["message_id"]

    # Once the send completes, mark_provisional applies the parked receipt straight to delivered.
    async def _terminal() -> dict | None:
        record = await bridge.get_record(route_name, message_id)
        return record if record["delivery_status"] == "delivered" else None

    final = await wait_for_async(
        _terminal, deadline=20.0, message=f"record {message_id} never settled delivered after the send completed"
    )
    assert final["answer"] == answer, "the answer must survive the receipt race intact"
    assert final.get("pending_receipt") is None, "the parked receipt is cleared once applied"

    # The parked receipt corresponds to already-ledgered chunks: every chunk was sent exactly
    # once, the outbound ids account for each, and nothing is re-sent after the record delivered.
    sent_count = len(bridge.fake_twilio.sends_matching(marker))
    assert sent_count == len(final["outbound_message_ids"]), (
        f"{sent_count} sends but {len(final['outbound_message_ids'])} ledgered outbound ids"
    )
    stable = await wait_channel_send_count(bridge.fake_twilio, marker, sent_count, deadline=5.0)
    assert len(stable) == sent_count, "a chunk was re-sent after the record was delivered"

    # The caller-scoped projection withholds the delivery bookkeeping: pending_receipt is
    # admin-only. A grant-holder reads the transcript (grant-gated, unlike the admin-only
    # listing) and gets the caller_view, which never carries the field.
    scoped = bridge.api(token=await bridge.mint_key(user_id=uniq("rr-scoped"), scopes=["e2e-all"]))
    thread_id = snapshot["thread_id"]
    caller_transcript = await scoped.get(_transcript_path(route_name, thread_id))
    caller_items = [item for item in caller_transcript["items"] if marker in (item.get("answer") or "")]
    assert caller_items, caller_transcript
    assert all("pending_receipt" not in item for item in caller_items), caller_items
