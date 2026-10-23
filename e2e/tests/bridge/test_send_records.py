"""Every message the platform sends to a participant is a record in the participant's thread.

A ``notify_user`` made during a turn to that turn's own participant sits in the turn's thread,
naming the turn it was made in; one made outside any turn starts the thread of the address it
went to, and the participant's next message continues that thread. An ask is one record holding
the question and the reply that came over the channel; a sensitive ask's record keeps that a
reply came and never its body; an ask answered by the operator holds no reply. The uniform
could-not-deliver notice is a send record of its own. A vendor status for a send's id settles
the send record, and a reply refused part-way keeps the ids of the parts already accepted.

Every leg reads the records back through the admin transcript door, so ``sends`` (the per-part
provider ids) is present.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import secrets
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import urlencode

import pytest

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_FROM, BRIDGE_WHATSAPP_PHONE_ID
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import WaitTimeoutError, wait_for_async

from ._bridge_support import (
    ERROR_ANSWER_TEXT,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    cancel_and_join,
    post_inbound,
    script_reply,
    tool_text,
    wait_channel_send_count,
    wait_record_status,
    wait_twilio_send,
    wait_whatsapp_send,
)

pytestmark = [
    pytest.mark.needs(
        "kind:channels:twilio",
        "kind:channels:whatsapp",
        "kind:identity",
        "probe-tools",
        "helper:llm",
        "helper:twilio",
        "helper:whatsapp",
        "setting:agent",
        "setting:conversations",
        "setting:seeded-access-control",
        "setting:router:notifications",
        "setting:tool:ask",
        "setting:tool:notify_user",
    ),
    pytest.mark.skipif(
        HarnessSettings().is_real("twilio")
        or HarnessSettings().is_real("whatsapp")
        or HarnessSettings().is_real("llm"),
        reason="FakeTwilio + FakeWhatsApp + scripted-LLM are the 'twilio'/'whatsapp'/'llm' mock leg",
    ),
]

_AGENT = "tools_agent"
# Over the ten-chunk ceiling of a twilio reply (1600 characters a chunk), so the channel refuses it.
_REFUSED_ANSWER_CHARS = 16_500
# Enough tokens to split a twilio reply into several chunks, the marker in every chunk.
_LONG_ANSWER_TOKENS = 500


def _random_identity() -> str:
    return f"+1555{secrets.randbelow(10**7):07d}"


def _transcript_path(route_name: str, thread_id: str) -> str:
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id, 'order': 'asc'})}"


def _thread_id(route_name: str, address: str) -> str:
    return f"bridge:{route_name}:{address}"


async def _transcript(bridge: BridgeHarness, route_name: str, address: str) -> list[dict[str, Any]]:
    return (await bridge.api().get(_transcript_path(route_name, _thread_id(route_name, address))))["items"]


async def _wait_transcript(
    bridge: BridgeHarness,
    route_name: str,
    address: str,
    predicate: Callable[[list[dict[str, Any]]], bool],
    *,
    deadline: float = 15.0,
) -> list[dict[str, Any]]:
    """The thread's records once ``predicate`` holds for them; a timeout names the records last read."""
    seen: list[Any] = []

    async def probe() -> list[dict[str, Any]] | None:
        # A thread no record has reached yet reads the uniform 404.
        response = await bridge.api().request_raw("GET", _transcript_path(route_name, _thread_id(route_name, address)))
        if response.status_code == 404:
            return None
        assert response.status_code == 200, response.text
        items = response.json()["data"]["items"]
        seen[:] = [items]
        return items if predicate(items) else None

    try:
        return await wait_for_async(probe, deadline=deadline, message="the thread never settled")
    except WaitTimeoutError as exc:
        raise AssertionError(f"the thread of {address} on {route_name} never settled; last read: {seen!r}") from exc


async def _agent_route(bridge: BridgeHarness, uniq: Callable[[str], str], identity: str, *, channel: str) -> str:
    route_name = uniq("sr-route").replace("_", "-")
    execution_key = uniq("sr-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name, agent=_AGENT, execution_key=execution_key, channel=channel, our_identity=identity
    )
    return route_name


@contextlib.asynccontextmanager
async def _default_route(
    bridge: BridgeHarness,
    uniq: Callable[[str], str],
    *,
    channel: str,
    tool: str | None = None,
    tool_start: str = "",
    tool_reply: str = "",
) -> AsyncIterator[str]:
    """A route on the deployment's own sending identity, deleted on exit.

    An ask and a ``notify_user`` go from that identity (a caller never names the identity a send
    goes from), and only one route may hold a ``(channel, our_identity)`` pair, so each leg that
    needs it takes it in turn. The route serves the agent, or with ``tool`` that tool, started
    with ``tool_start`` and answering ``tool_reply``.
    """
    identity = BRIDGE_TWILIO_FROM if channel == "twilio" else BRIDGE_WHATSAPP_PHONE_ID
    if tool is None:
        route_name = await _agent_route(bridge, uniq, identity, channel=channel)
    else:
        route_name = uniq("sr-tool-route").replace("_", "-")
        execution_key = uniq("sr-tool-exec")
        await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
        await bridge.create_tool_channel_route(
            route_name=route_name,
            tool=tool,
            execution_key=execution_key,
            channel=channel,
            our_identity=identity,
            start_expr=tool_start,
            reply_expr=f'"{tool_reply}"',
        )
    try:
        yield route_name
    finally:
        await bridge.api().delete(f"/api/conversations/{route_name}")


async def _twilio_message(bridge: BridgeHarness, identity: str, text: str) -> None:
    port = bridge.stack.port_b
    inbound = bridge.twilio_inbound(our_identity=identity, client=BRIDGE_TWILIO_CLIENT, text=text, port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204


async def _call_tool(bridge: BridgeHarness, name: str, arguments: dict[str, Any]) -> Any:
    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        return await mcp.call_tool(name, arguments)


# -- notify_user ------------------------------------------------------------------------------


async def test_a_mid_turn_notify_sits_in_its_turn(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    notice = uniq("sr-mid")
    reply = uniq("sr-mid-reply")
    start = f'{{message: "{notice}", channel: "twilio", recipient: .sender}}'
    async with _default_route(
        bridge, uniq, channel="twilio", tool="notify_user", tool_start=start, tool_reply=reply
    ) as route_name:
        await _twilio_message(bridge, BRIDGE_TWILIO_FROM, "hello")
        sent = await wait_twilio_send(bridge.fake_twilio, notice)
        await wait_twilio_send(bridge.fake_twilio, reply)

        turn, send = await _wait_transcript(
            bridge, route_name, BRIDGE_TWILIO_CLIENT, lambda items: len(items) == 2 and items[0]["answer"] == reply
        )
    assert (turn["origin"], turn["inbound_text"]) == ("client", "hello")
    assert (send["origin"], send["sent_by"], send["inbound_text"], send["answer"]) == ("send", "notify", "", notice)
    assert send["turn_message_id"] == turn["message_id"]
    assert turn["created_at"] <= send["created_at"] <= turn["updated_at"]
    assert send["sends"] == [{"part": 0, "chars": len(notice), "provider_message_ids": [sent["sid"]]}]


async def test_an_out_of_turn_notify_starts_the_thread_and_the_next_message_continues_it(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    async with _default_route(bridge, uniq, channel="twilio") as route_name:
        notice = uniq("sr-out")
        result = await _call_tool(
            bridge, "notify_user", {"message": notice, "channel": "twilio", "recipient": BRIDGE_TWILIO_CLIENT}
        )
        assert "notification sent via 'twilio'" in tool_text(result)
        sent = await wait_twilio_send(bridge.fake_twilio, notice)

        (listed,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
        assert listed["thread_id"] == _thread_id(route_name, BRIDGE_TWILIO_CLIENT)
        (send,) = await _transcript(bridge, route_name, BRIDGE_TWILIO_CLIENT)
        assert (send["origin"], send["sent_by"], send["answer"], send["turn_message_id"]) == (
            "send",
            "notify",
            notice,
            None,
        )
        assert send["sends"] == [{"part": 0, "chars": len(notice), "provider_message_ids": [sent["sid"]]}]

        answer = uniq("sr-out-answer")
        script_reply(bridge.llm_stub, answer)
        await _twilio_message(bridge, BRIDGE_TWILIO_FROM, "thanks")
        await wait_twilio_send(bridge.fake_twilio, answer)

        first, turn = await _wait_transcript(
            bridge, route_name, BRIDGE_TWILIO_CLIENT, lambda items: len(items) == 2 and items[1]["answer"] == answer
        )
        assert first["message_id"] == send["message_id"]
        assert (turn["origin"], turn["inbound_text"]) == ("client", "thanks")
        assert (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["total"] == 1


# -- ask --------------------------------------------------------------------------------------


async def _ask_record(bridge: BridgeHarness, route_name: str, question: str) -> dict[str, Any]:
    (record,) = await _wait_transcript(
        bridge, route_name, BRIDGE_TWILIO_CLIENT, lambda items: len(items) == 1 and question in items[0]["answer"]
    )
    return record


async def _ask_answered_over_the_channel(
    bridge: BridgeHarness, uniq: Callable[[str], str], tool: str, arguments: dict[str, Any]
) -> tuple[str, str, dict[str, Any]]:
    """Run ``tool`` asking a question over twilio and reply to it from the participant's number.

    Answers ``(route_name, reply, record)``, the record read once it is ``delivered``. The ``ask``
    tool returns the answer object, whose ``answer`` is the reply; ``e2e_sensitive_ask`` returns
    the reply itself.
    """
    question = arguments["question"]
    reply = uniq("sr-reply")
    async with _default_route(bridge, uniq, channel="twilio") as route_name:
        task = asyncio.create_task(_call_tool(bridge, tool, arguments))
        try:
            await wait_twilio_send(bridge.fake_twilio, question)
            await _twilio_message(bridge, BRIDGE_TWILIO_FROM, reply)
            result = await asyncio.wait_for(task, timeout=15.0)
        finally:
            await cancel_and_join(task)
        assert (result.data.answer if tool == "ask" else result.data) == reply
        record = await _ask_record(bridge, route_name, question)
        delivered = await wait_record_status(bridge, route_name, record["message_id"], {"delivered"})
        transcript = await _transcript(bridge, route_name, BRIDGE_TWILIO_CLIENT)
        assert len(transcript) == 1, "the reply to the ask started a turn of its own"
        search = await bridge.api().get(f"/api/conversations/{route_name}/messages/search?{urlencode({'q': reply})}")
        if tool == "ask":
            assert [item["message_id"] for item in search["items"]] == [record["message_id"]]
        else:
            assert search["items"] == []
            assert reply not in json.dumps(transcript)
    return route_name, reply, delivered


async def test_an_ask_and_its_channel_reply_are_one_delivered_record(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("sr-ask")

    _route_name, reply, record = await _ask_answered_over_the_channel(
        bridge, uniq, "ask", {"question": question, "channel": "twilio"}
    )

    assert (record["origin"], record["sent_by"], record["turn_message_id"]) == ("send", "ask", None)
    assert question in record["answer"]
    assert record["ask"]["sensitive"] is False
    assert record["ask_reply"]["answer"] == reply
    assert record["ask_reply"]["medium_message_id"]


async def test_a_sensitive_ask_keeps_that_a_reply_came_and_never_its_body(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    question = uniq("sr-sensitive")

    _route_name, reply, record = await _ask_answered_over_the_channel(
        bridge, uniq, "e2e_sensitive_ask", {"question": question, "channel": "twilio"}
    )

    assert (record["origin"], record["sent_by"]) == ("send", "ask")
    assert record["ask"]["sensitive"] is True
    assert record["ask_reply"]["answer"] is None
    assert record["ask_reply"]["answered_at"]
    assert reply not in json.dumps(record)


async def test_an_ask_the_operator_answers_holds_no_reply(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    question = uniq("sr-operator-ask")
    answer = uniq("sr-operator-answer")
    async with _default_route(bridge, uniq, channel="twilio") as route_name:
        task = asyncio.create_task(_call_tool(bridge, "ask", {"question": question, "channel": "twilio"}))
        try:
            await wait_twilio_send(bridge.fake_twilio, question)
            record = await _ask_record(bridge, route_name, question)
            interaction_id = record["ask"]["interaction_id"]
            await bridge.api().post(f"/api/interactions/{interaction_id}/answer", json={"answer": answer})
            result = await asyncio.wait_for(task, timeout=15.0)
        finally:
            await cancel_and_join(task)
        assert result.data.answer == answer

        (after,) = await _transcript(bridge, route_name, BRIDGE_TWILIO_CLIENT)
    assert (after["message_id"], after["sent_by"]) == (record["message_id"], "ask")
    assert after["ask_reply"] is None


# -- notices, receipts and partial failures ----------------------------------------------------


async def test_the_could_not_deliver_notice_is_a_send_record(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    identity = _random_identity()
    route_name = await _agent_route(bridge, uniq, identity, channel="twilio")
    script_reply(bridge.llm_stub, "x" * _REFUSED_ANSWER_CHARS)

    await _twilio_message(bridge, identity, "tell me everything")
    sent = await wait_twilio_send(bridge.fake_twilio, ERROR_ANSWER_TEXT)

    turn, notice = await _wait_transcript(
        bridge,
        route_name,
        BRIDGE_TWILIO_CLIENT,
        lambda items: len(items) == 2 and items[1]["delivery_status"] != "sending",
    )
    assert (turn["origin"], turn["delivery_status"]) == ("client", "failed")
    assert (notice["origin"], notice["sent_by"], notice["answer"]) == ("send", "notice", ERROR_ANSWER_TEXT)
    assert notice["sends"] == [{"part": 0, "chars": len(ERROR_ANSWER_TEXT), "provider_message_ids": [sent["sid"]]}]


async def test_a_whatsapp_status_for_a_send_settles_its_record(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    notice = uniq("sr-wa")
    wa_id = f"1650{uuid.uuid4().int % 10**9:09d}"
    async with _default_route(bridge, uniq, channel="whatsapp") as route_name:
        await _call_tool(bridge, "notify_user", {"message": notice, "channel": "whatsapp", "recipient": wa_id})
        sent = await wait_whatsapp_send(bridge.fake_whatsapp, notice)
        (record,) = await _wait_transcript(bridge, route_name, wa_id, lambda items: len(items) == 1)
        assert record["sends"] == [{"part": 0, "chars": len(notice), "provider_message_ids": [sent["wamid"]]}]
        assert record["delivery_status"] == "provisional"

        status = bridge.whatsapp_status(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wamid=sent["wamid"], status="delivered", recipient_id=wa_id
        )
        assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, status)).status_code == 200

        settled = await wait_record_status(bridge, route_name, record["message_id"], {"delivered"})
    assert settled["sends"] == record["sends"]


async def test_a_reply_refused_part_way_keeps_the_ids_of_the_parts_sent(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = _random_identity()
    route_name = await _agent_route(bridge, uniq, identity, channel="twilio")
    marker = uniq("sr-partial")
    script_reply(bridge.llm_stub, " ".join(f"{marker}-{index:04d}" for index in range(_LONG_ANSWER_TOKENS)))

    async def refuse_after_the_first_chunk() -> None:
        # The stub has already kept the chunk it is refusing; the could-not-deliver notice
        # that follows the failed reply is let through.
        if marker in bridge.fake_twilio.messages[-1]["body"] and len(bridge.fake_twilio.sends_matching(marker)) >= 2:
            raise RuntimeError("the twilio stub refuses every chunk after the first")

    bridge.fake_twilio.send_barrier = refuse_after_the_first_chunk
    try:
        await _twilio_message(bridge, identity, "tell me a lot")
        first = (await wait_channel_send_count(bridge.fake_twilio, marker, 2))[0]
        notice_sent = await wait_twilio_send(bridge.fake_twilio, ERROR_ANSWER_TEXT)

        record, notice = await _wait_transcript(
            bridge,
            route_name,
            BRIDGE_TWILIO_CLIENT,
            lambda items: len(items) == 2 and items[1]["delivery_status"] != "sending",
        )
    finally:
        bridge.fake_twilio.send_barrier = None
    assert record["delivery_status"] == "failed"
    assert record["sends"] == [{"part": 0, "chars": len(first["body"]), "provider_message_ids": [first["sid"]]}]
    assert (notice["origin"], notice["sent_by"], notice["answer"]) == ("send", "notice", ERROR_ANSWER_TEXT)
    assert notice["sends"] == [
        {"part": 0, "chars": len(ERROR_ANSWER_TEXT), "provider_message_ids": [notice_sent["sid"]]}
    ]
