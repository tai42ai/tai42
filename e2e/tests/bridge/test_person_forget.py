"""Erasing a linked person over a real bridge conversation.

A ``/link`` handshake folds a twilio address and a whatsapp address into ONE person, whose
post-link turns key the aggregated ``bridge:@person:{id}`` thread. The operator then erases
that person through ``DELETE /api/conversations/persons/{person_id}``, and the proof it worked
is threefold:

1. the aggregated thread is gone — its transcript reads the uniform 404 and the route listing
   no longer carries it (records + indexes erased);
2. the person row + its address→person index mappings are gone — the SAME twilio address's
   next message mints a FRESH provisional person id, so nothing remembers it was ever linked;
3. a second erase of the same id is idempotent (``erased: false``), never a 404.

Linking mechanics mirror ``test_tool_person_continuity``; the erase mirrors the
thread-forget door of ``test_thread_delete``.

The erase also clears every person a link absorbed into the erased one. An absorbed id is
refused on its own (409, naming the person that absorbed it), and the survivor's erase clears
the absorbed person's thread, every chat the survivor's and the absorbed person's addresses had
before the link, and a thread only an operator send reached; it reaches a route an ``/unlink``
took off the person, and leaves a chat another person continued after an ``/unlink``. Each of
those legs reads the stack's Redis directly afterwards for any key that still names the cleared
records.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import subprocess
import sys
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlencode

import pytest
import redis
from tai42_skeleton.conversations.settings import ConversationsSettings
from tai42_skeleton.conversations.thread_ids import RouteThreadId, parse_thread_id

from tai42_e2e.binaries import tai_bin
from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import (
    LINK_REPLY_PREFIX,
    LINKED_TEXT,
    SEND_KEY_FAMILIES,
    TWILIO_INBOUND_PATH,
    UNLINKED_TEXT,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    extract_pair_code,
    post_inbound,
    redis_keys_naming,
    script_reply,
    wait_record_key_for_value,
    wait_send_to,
)

pytestmark = [
    pytest.mark.needs(
        "kind:channels:twilio",
        "kind:channels:whatsapp",
        "kind:identity",
        "probe-tools",
        "mutable",
        "helper:llm",
        "helper:twilio",
        "helper:whatsapp",
        "setting:agent",
        "setting:conversations",
        "setting:router:notifications",
        "setting:tool:notify_user",
        "setting:seeded-access-control",
        "store:redis",
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("twilio", "whatsapp", "llm")),
        reason="FakeTwilio + FakeWhatsApp + scripted-LLM are the 'twilio'/'whatsapp'/'llm' mock leg",
    ),
]

_TOOL = "e2e_record"
# The person the turn ran as keys the probe list; the message is its value, so a per-fire
# marker resolves the person id the turn minted.
_PAYLOAD_EXPR = "{key: .person_id, value: .message}"


_AGENT = "tools_agent"
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _transcript_path(route_name: str, thread_id: str) -> str:
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id, 'order': 'asc'})}"


async def test_erasing_a_linked_person_forgets_every_person_scoped_store(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    await bridge.set_target_config(target_kind="tool", target_name=_TOOL, multichannel=True)
    exec_tw = uniq("l25-exec-tw")
    exec_wa = uniq("l25-exec-wa")
    await bridge.mint_key(user_id=exec_tw, scopes=["e2e-all"])
    await bridge.mint_key(user_id=exec_wa, scopes=["e2e-all"])
    route_tw = uniq("l25-route-tw").replace("_", "-")
    route_wa = uniq("l25-route-wa").replace("_", "-")
    await bridge.create_tool_channel_route(
        route_name=route_tw,
        tool=_TOOL,
        execution_key=exec_tw,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        start_expr=_PAYLOAD_EXPR,
        reply_expr="null",
    )
    await bridge.create_tool_channel_route(
        route_name=route_wa,
        tool=_TOOL,
        execution_key=exec_wa,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
        start_expr=_PAYLOAD_EXPR,
        reply_expr="null",
    )
    try:
        await _erase_a_linked_person(bridge, uniq, route_tw)
    finally:
        # The routes hold the deployment's own twilio number and whatsapp phone id, which the
        # other legs take in turn.
        for route_name in (route_tw, route_wa):
            await bridge.api().delete(f"/api/conversations/{route_name}")


async def _erase_a_linked_person(bridge: BridgeHarness, uniq: Callable[[str], str], route_tw: str) -> None:
    port = bridge.stack.port_b

    async def twilio_msg(text: str) -> None:
        inbound = bridge.twilio_inbound(
            our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text=text, port=port
        )
        assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204

    async def whatsapp_msg(text: str) -> None:
        inbound = bridge.whatsapp_inbound(
            phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=text
        )
        assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound, port=port)).status_code == 200

    # Two provisional persons pre-link, then a /link + redeem merges them into one.
    await twilio_msg(uniq("l25-tw1"))
    await whatsapp_msg(uniq("l25-wa1"))
    link_inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="/link", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, link_inbound, port=port)).status_code == 204
    (link_reply,) = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, needle=LINK_REPLY_PREFIX)
    code = extract_pair_code(link_reply["body"])
    redeem_inbound = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=code
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, redeem_inbound, port=port)).status_code == 200
    (linked_reply,) = await wait_send_to(bridge.fake_whatsapp, to=BRIDGE_WHATSAPP_CLIENT, needle=LINKED_TEXT)
    assert linked_reply["body"] == LINKED_TEXT

    # A post-link twilio turn writes under the survivor id and keys its aggregated thread.
    marker = uniq("l25-linked")
    await twilio_msg(marker)
    survivor = await wait_record_key_for_value(bridge, marker)
    person_thread = f"bridge:@person:{survivor}"
    transcript = await bridge.api().get(_transcript_path(route_tw, person_thread))
    assert marker in [item["inbound_text"] for item in transcript["items"]], transcript["items"]

    # Erase the person: the aggregated thread across both routes, the person row, its index
    # mappings. The removed count spans the routes the person wrote under.
    erased = await bridge.api().delete(f"/api/conversations/persons/{survivor}")
    assert erased["person_id"] == survivor
    assert erased["erased"] is True
    assert erased["removed"] >= 1

    # (1) the aggregated thread is gone: the transcript is the uniform 404 and the listing no
    # longer carries it.
    await bridge.api().get(_transcript_path(route_tw, person_thread), expect=404)
    listing = await bridge.api().get(f"/api/conversations/{route_tw}/threads")
    assert person_thread not in [item["thread_id"] for item in listing["items"]]

    # (2) the person row + index mappings are gone: the same twilio address now mints a FRESH
    # provisional person, so nothing remembers the erased link.
    reborn_marker = uniq("l25-reborn")
    await twilio_msg(reborn_marker)
    reborn = await wait_record_key_for_value(bridge, reborn_marker)
    assert reborn != survivor, f"the erased person's id survived into a new turn ({survivor!r})"

    # (3) a second erase of the same id is idempotent — not a 404.
    again = await bridge.api().delete(f"/api/conversations/persons/{survivor}")
    assert again == {
        "person_id": survivor,
        "removed": 0,
        "erased": False,
        "states_erased": 0,
        "absorbed_person_ids": [],
    }


# -- the persons a link absorbed ----------------------------------------------------------------

# The reply every ``e2e_record`` turn on the linked legs' routes answers, so each message has a
# reply send whose ids the key scan looks for too.
_NOTED = "noted"
# Every key family that names a record, a thread or a person: the records and their send keys,
# the transcript and route indexes, each writer's threads, and the person row with its absorbed
# ids, its pointer and its recorded routes.
_THREAD_FAMILIES = (*SEND_KEY_FAMILIES, "thread:", "route_threads:", "writer_threads:")
_PERSON_FAMILIES = (
    "person:",
    "person_absorbed:",
    "person_absorbed_into:",
    "person_thread_routes:",
    "writer_threads:",
)


def _random_twilio_address() -> str:
    return f"+1555{secrets.randbelow(10**7):07d}"


def _random_wa_id() -> str:
    return f"1650{secrets.randbelow(10**9):09d}"


def _absorbed_refusal(absorbed: str, survivor: str) -> str:
    return (
        f"conversation person {absorbed!r} was absorbed into person {survivor!r} by a link and is not erased "
        f"on its own: erase {survivor!r}, which clears {survivor!r} and every person it absorbed, "
        f"{absorbed!r} included"
    )


@dataclass
class _Linked:
    """A twilio and a whatsapp ``e2e_record`` route on the deployment's own identities, and the
    conversations on them that the legs drive."""

    bridge: BridgeHarness
    uniq: Callable[[str], str]
    route_tw: str
    route_wa: str

    def thread(self, channel: str, address: str) -> str:
        return f"bridge:{self.route_tw if channel == 'twilio' else self.route_wa}:{address}"

    async def send(self, channel: str, address: str, text: str) -> None:
        port = self.bridge.stack.port_b
        if channel == "twilio":
            inbound = self.bridge.twilio_inbound(our_identity=BRIDGE_TWILIO_FROM, client=address, text=text, port=port)
            assert (await post_inbound(self.bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204
        else:
            inbound = self.bridge.whatsapp_inbound(phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=address, text=text)
            assert (await post_inbound(self.bridge.stack, WHATSAPP_INBOUND_PATH, inbound, port=port)).status_code == 200

    def fake(self, channel: str) -> Any:
        return self.bridge.fake_twilio if channel == "twilio" else self.bridge.fake_whatsapp

    async def write(self, channel: str, address: str) -> str:
        """One message from ``address`` and its reply; answers the person id its turn ran as."""
        marker = self.uniq("l25-msg")
        replies = len([sent for sent in self.fake(channel).sends_matching(_NOTED) if sent.get("to") == address])
        await self.send(channel, address, marker)
        person = await wait_record_key_for_value(self.bridge, marker)
        await wait_send_to(self.fake(channel), to=address, needle=_NOTED, count=replies + 1)
        return person

    async def link(self, minting: tuple[str, str], redeeming: tuple[str, str]) -> None:
        """``/link`` from the ``minting`` conversation, its code redeemed from the ``redeeming`` one."""
        await self.send(*minting, "/link")
        (link_reply,) = await wait_send_to(self.fake(minting[0]), to=minting[1], needle=LINK_REPLY_PREFIX)
        await self.send(*redeeming, extract_pair_code(link_reply["body"]))
        await wait_send_to(self.fake(redeeming[0]), to=redeeming[1], needle=LINKED_TEXT)

    async def unlink(self, channel: str, address: str) -> None:
        await self.send(channel, address, "/unlink")
        await wait_send_to(self.fake(channel), to=address, needle=UNLINKED_TEXT)

    async def items(self, thread_id: str) -> list[dict[str, Any]]:
        """The thread's records once none is still sending; ``[]`` for a thread that reads the 404.

        A route-keyed thread is read under its own route. A person thread is read under the twilio
        route, so its aggregate spans the routes of the person's addresses.
        """
        parsed = parse_thread_id(thread_id)
        route_name = parsed.route_name if isinstance(parsed, RouteThreadId) else self.route_tw

        async def settled() -> tuple[list[dict[str, Any]]] | None:
            response = await self.bridge.api().request_raw("GET", _transcript_path(route_name, thread_id))
            if response.status_code == 404:
                return ([],)
            assert response.status_code == 200, response.text
            items = response.json()["data"]["items"]
            return None if any(item["delivery_status"] == "sending" for item in items) else (items,)

        (items,) = await wait_for_async(settled, deadline=15.0, message=f"the records of {thread_id} never settled")
        return items

    def indexed(self, thread_id: str) -> set[str]:
        """The record ids both routes' indexes of ``thread_id`` hold, read from the stack's Redis.

        A person thread's records on a route none of its addresses names any more are in that
        route's index but out of the transcript, which spans the addresses' routes only.
        """
        settings = ConversationsSettings(prefix=self.bridge.stack.config.env["CONVERSATIONS_PREFIX"])
        client = redis.Redis.from_url(self.bridge.stack.resources.redis_url, decode_responses=True)
        try:
            return {
                message_id
                for route_name in (self.route_tw, self.route_wa)
                for message_id in cast(
                    "list[str]", client.zrange(settings.keyspace.thread_index_key(route_name, thread_id), 0, -1)
                )
            }
        finally:
            client.close()


@contextlib.asynccontextmanager
async def _linked_routes(bridge: BridgeHarness, uniq: Callable[[str], str]) -> AsyncIterator[_Linked]:
    """The two routes, deleted on exit: each holds a deployment identity the other legs take in turn."""
    await bridge.set_target_config(target_kind="tool", target_name=_TOOL, multichannel=True)
    routes: dict[str, str] = {}
    try:
        for channel, identity in (("twilio", BRIDGE_TWILIO_FROM), ("whatsapp", BRIDGE_WHATSAPP_PHONE_ID)):
            execution_key = uniq(f"l25-exec-{channel}")
            await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
            routes[channel] = uniq(f"l25-route-{channel}").replace("_", "-")
            await bridge.create_tool_channel_route(
                route_name=routes[channel],
                tool=_TOOL,
                execution_key=execution_key,
                channel=channel,
                our_identity=identity,
                start_expr=_PAYLOAD_EXPR,
                reply_expr=f'"{_NOTED}"',
            )
        yield _Linked(bridge, uniq, routes["twilio"], routes["whatsapp"])
    finally:
        for route_name in routes.values():
            await bridge.api().delete(f"/api/conversations/{route_name}")


def _needles(items: list[dict[str, Any]]) -> list[str]:
    """What names a thread's records in a key: each record's id and its parts' provider ids."""
    return [
        *(item["message_id"] for item in items),
        *(sid for item in items for sent in item["sends"] for sid in sent["provider_message_ids"]),
    ]


async def test_erasing_a_person_clears_the_person_it_absorbed(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    p_tw, q_tw, q_wa = _random_twilio_address(), _random_twilio_address(), _random_wa_id()
    async with _linked_routes(bridge, uniq) as linked:
        p = await linked.write("twilio", p_tw)
        q = await linked.write("twilio", q_tw)
        w = await linked.write("whatsapp", q_wa)
        await linked.link(("twilio", q_tw), ("whatsapp", q_wa))
        assert await linked.write("twilio", q_tw) == q
        notice = uniq("l25-notice")
        async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
            await mcp.call_tool("notify_user", {"message": notice, "channel": "whatsapp", "recipient": q_wa})
        await wait_send_to(bridge.fake_whatsapp, to=q_wa, needle=notice)
        q_thread = f"bridge:@person:{q}"
        q_items = await linked.items(q_thread)
        await linked.link(("twilio", p_tw), ("twilio", q_tw))

        assert ("send", "notify", notice) in [(i["origin"], i["sent_by"], i["answer"]) for i in q_items], q_items
        needles = [*_needles(q_items), q_thread]
        before = redis_keys_naming(bridge, needles, families=_THREAD_FAMILIES)
        assert before, "the scan found no key naming the absorbed person's thread"

        refused = await bridge.api().request_raw("DELETE", f"/api/conversations/persons/{q}")
        assert refused.status_code == 409, refused.text
        assert refused.json() == {"error": _absorbed_refusal(q, p), "absorbed_into_person_id": p}
        assert redis_keys_naming(bridge, needles, families=_THREAD_FAMILIES) == before

        erased = await bridge.api().delete(f"/api/conversations/persons/{p}")
        # The link flattens what Q absorbed into P, so the erase names both.
        assert (erased["person_id"], erased["erased"], erased["absorbed_person_ids"]) == (p, True, sorted([q, w]))
        assert redis_keys_naming(bridge, needles, families=_THREAD_FAMILIES) == {}
        assert redis_keys_naming(bridge, [p, q, w], families=_PERSON_FAMILIES) == {}


def _run_cli(bridge: BridgeHarness, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """``tai --server <replica A> --json <args...>``, authenticated by the root key."""
    env = {
        "PATH": os.pathsep.join([str(Path(sys.executable).parent), "/usr/local/bin", "/usr/bin", "/bin"]),
        "HOME": str(cwd),
        "TAI_API_KEY": bridge.root_token,
    }
    argv = [tai_bin(), "--server", bridge.stack.origin(bridge.stack.port_a), "--json", *args]
    return subprocess.run(argv, env=env, cwd=str(cwd), capture_output=True, text=True, timeout=90)


async def test_the_cli_erases_a_survivor_with_its_absorbed_ids_and_refuses_an_absorbed_id(
    bridge: BridgeHarness, uniq: Callable[[str], str], tmp_path: Path
) -> None:
    p_tw, q_tw = _random_twilio_address(), _random_twilio_address()
    async with _linked_routes(bridge, uniq) as linked:
        p = await linked.write("twilio", p_tw)
        q = await linked.write("twilio", q_tw)
        await linked.link(("twilio", p_tw), ("twilio", q_tw))

        refused = await asyncio.to_thread(_run_cli, bridge, tmp_path, "conversations", "delete-person", q)
        assert refused.returncode != 0, refused.stdout
        assert f"Error (HTTP 409): {_absorbed_refusal(q, p)}" in refused.stdout + refused.stderr

        erased = await asyncio.to_thread(_run_cli, bridge, tmp_path, "conversations", "delete-person", p)
        assert erased.returncode == 0, erased.stderr
        assert json.loads(erased.stdout)["absorbed_person_ids"] == [q]


async def test_erasing_a_person_clears_its_own_chat_and_the_absorbed_persons_chat_from_before_the_link(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    p_tw, q_wa = _random_twilio_address(), _random_wa_id()
    async with _linked_routes(bridge, uniq) as linked:
        p = await linked.write("twilio", p_tw)
        q = await linked.write("whatsapp", q_wa)
        await linked.link(("twilio", p_tw), ("whatsapp", q_wa))

        q_chat = linked.thread("whatsapp", q_wa)
        q_items = await linked.items(q_chat)
        p_items = await linked.items(linked.thread("twilio", p_tw))
        assert q_items
        assert p_items
        q_needles = [*_needles(q_items), q_chat]
        p_needles = [*_needles(p_items), linked.thread("twilio", p_tw)]

        erased = await bridge.api().delete(f"/api/conversations/persons/{p}")
        assert (erased["erased"], erased["absorbed_person_ids"], erased["removed"]) == (
            True,
            [q],
            len(q_items) + len(p_items),
        )
        assert redis_keys_naming(bridge, [*q_needles, *p_needles], families=_THREAD_FAMILIES) == {}
        assert await linked.items(q_chat) == []
        assert await linked.items(linked.thread("twilio", p_tw)) == []

        await linked.write("whatsapp", q_wa)
        (fresh,) = await linked.items(q_chat)
        assert (fresh["origin"], fresh["answer"]) == ("client", _NOTED)


async def test_erasing_a_person_leaves_a_chat_continued_after_an_unlink(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    p_tw, q_wa = _random_twilio_address(), _random_wa_id()
    async with _linked_routes(bridge, uniq) as linked:
        p = await linked.write("twilio", p_tw)
        q = await linked.write("whatsapp", q_wa)
        await linked.link(("twilio", p_tw), ("whatsapp", q_wa))
        await linked.unlink("whatsapp", q_wa)
        await linked.write("whatsapp", q_wa)

        q_chat = linked.thread("whatsapp", q_wa)
        q_items = await linked.items(q_chat)
        p_chat = linked.thread("twilio", p_tw)
        p_items = await linked.items(p_chat)
        person_thread = f"bridge:@person:{p}"
        # The ``/unlink`` keyed to the person's thread on the whatsapp route.
        person_records = linked.indexed(person_thread)
        assert person_records
        assert p_items

        erased = await bridge.api().delete(f"/api/conversations/persons/{p}")
        assert (erased["erased"], erased["absorbed_person_ids"]) == (True, [q])
        # The person's own chat from before the link goes with it; the chat continued after the
        # unlink is left.
        assert erased["removed"] == len(person_records) + len(p_items)
        assert linked.indexed(person_thread) == set()
        assert await linked.items(p_chat) == []
        assert await linked.items(q_chat) == q_items
        # The message and reply from before the link, and the one after the unlink.
        assert [item["answer"] for item in q_items].count(_NOTED) == 2


async def test_erasing_a_person_reaches_a_route_an_unlink_took_off_it(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    p_tw, q_wa = _random_twilio_address(), _random_wa_id()
    async with _linked_routes(bridge, uniq) as linked:
        p = await linked.write("twilio", p_tw)
        await linked.write("whatsapp", q_wa)
        await linked.link(("twilio", p_tw), ("whatsapp", q_wa))
        assert await linked.write("twilio", p_tw) == p
        assert await linked.write("whatsapp", q_wa) == p
        person_thread = f"bridge:@person:{p}"
        person_items = await linked.items(person_thread)
        assert {item["route_name"] for item in person_items} == {linked.route_tw, linked.route_wa}
        await linked.unlink("whatsapp", q_wa)
        person_records = linked.indexed(person_thread)
        assert {item["message_id"] for item in person_items} < person_records
        q_items = await linked.items(linked.thread("whatsapp", q_wa))
        p_items = await linked.items(linked.thread("twilio", p_tw))

        erased = await bridge.api().delete(f"/api/conversations/persons/{p}")
        assert erased["removed"] == len(person_records) + len(q_items) + len(p_items)
        settings = ConversationsSettings(prefix=bridge.stack.config.env["CONVERSATIONS_PREFIX"])
        client = redis.Redis.from_url(bridge.stack.resources.redis_url, decode_responses=True)
        try:
            assert not client.exists(settings.keyspace.thread_index_key(linked.route_wa, person_thread))
            assert client.zscore(settings.keyspace.route_threads_key(linked.route_wa), person_thread) is None
            assert not client.exists(settings.keyspace.person_thread_routes_key(p))
        finally:
            client.close()
        needles = [*_needles(person_items), *person_records]
        assert redis_keys_naming(bridge, needles, families=_THREAD_FAMILIES) == {}


async def test_erasing_a_person_clears_a_thread_only_an_operator_send_reached(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    await bridge.set_target_config(target_kind="agent", target_name=_AGENT, multichannel=True)
    identity = _random_twilio_address()
    address = _random_twilio_address()
    routes = [uniq(f"l25-route-{name}").replace("_", "-") for name in ("tw", "r1", "r2")]
    execution_key = uniq("l25-exec-agent")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=routes[0], agent=_AGENT, execution_key=execution_key, channel="twilio", our_identity=identity
    )
    for route_name in routes[1:]:
        await bridge.create_api_route(
            route_name=route_name, agent=_AGENT, execution_key=execution_key, callback_url=_UNREACHABLE_CALLBACK
        )
    try:
        port = bridge.stack.port_b

        async def twilio_msg(text: str) -> None:
            inbound = bridge.twilio_inbound(our_identity=identity, client=address, text=text, port=port)
            assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204

        first = uniq("l25-first")
        script_reply(bridge.llm_stub, first)
        await twilio_msg("hello")
        await wait_send_to(bridge.fake_twilio, to=address, needle=first)

        principal = uniq("l25-caller")
        caller = bridge.api(token=await bridge.mint_key(user_id=principal, scopes=["e2e-all"]))
        end_user = uniq("l25-user")
        api_address = f"{quote(principal, safe='')}/{end_user}"
        script_reply(bridge.llm_stub, uniq("l25-api-answer"))
        await caller.post(
            f"/api/conversations/{routes[1]}/messages",
            json={"external_user_id": end_user, "text": "hello", "wait_seconds": 20},
        )
        r2_thread = f"bridge:{routes[2]}:{api_address}"
        sent = await bridge.api().post(
            f"/api/conversations/{routes[2]}/thread/messages",
            json={"thread_id": r2_thread, "text": uniq("l25-operator")},
        )
        assert sent["thread_id"] == r2_thread

        await twilio_msg("/link")
        (link_reply,) = await wait_send_to(bridge.fake_twilio, to=address, needle=LINK_REPLY_PREFIX)
        redeem = await caller.post(
            f"/api/conversations/{routes[1]}/messages",
            json={"external_user_id": end_user, "text": extract_pair_code(link_reply["body"]), "wait_seconds": 20},
        )
        assert redeem["answer"]["answer"] == LINKED_TEXT

        async def r2_settled() -> list[dict[str, Any]] | None:
            items = (await bridge.api().get(_transcript_path(routes[2], r2_thread)))["items"]
            return None if any(item["delivery_status"] == "sending" for item in items) else items

        (operator_send,) = await wait_for_async(r2_settled, deadline=15.0, message="the operator send never settled")
        assert operator_send["message_id"] == sent["message_id"]
        api_chat = f"bridge:{routes[1]}:{api_address}"
        api_items = (await bridge.api().get(_transcript_path(routes[1], api_chat)))["items"]

        # A message after the link keys the person thread, which names the survivor.
        after = uniq("l25-after")
        script_reply(bridge.llm_stub, after)
        await twilio_msg("again")
        await wait_send_to(bridge.fake_twilio, to=address, needle=after)
        listing = (await bridge.api().get(f"/api/conversations/{routes[0]}/threads"))["items"]
        (person_thread,) = [item["thread_id"] for item in listing if item["thread_id"].startswith("bridge:@person:")]
        person_items = (await bridge.api().get(_transcript_path(routes[0], person_thread)))["items"]
        twilio_chat = f"bridge:{routes[0]}:{address}"
        twilio_items = (await bridge.api().get(_transcript_path(routes[0], twilio_chat)))["items"]

        erased = await bridge.api().delete(
            f"/api/conversations/persons/{person_thread.removeprefix('bridge:@person:')}"
        )
        assert erased["erased"] is True
        assert len(erased["absorbed_person_ids"]) == 1
        assert erased["removed"] == len(person_items) + len(api_items) + len(twilio_items) + 1
        assert redis_keys_naming(bridge, [operator_send["message_id"], r2_thread], families=_THREAD_FAMILIES) == {}
        await bridge.api().get(_transcript_path(routes[2], r2_thread), expect=404)
        await bridge.api().get(_transcript_path(routes[0], twilio_chat), expect=404)
    finally:
        for route_name in routes:
            await bridge.api().delete(f"/api/conversations/{route_name}")
