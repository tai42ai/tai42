"""The same channel message delivered twice at once becomes one record and one turn.

A channel intake writes its record, claims the inbound pair and refreshes the thread mode in one
atomic step, so two concurrent deliveries of one message (the web channel's retry key re-derives the
same provider message id) race on the claim: one wins and runs the turn, the other writes nothing
and is acknowledged with the winner's message id.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness

pytestmark = [
    pytest.mark.needs(
        "kind:channels:web",
        "kind:identity",
        "probe-tools",
        "setting:conversations",
        "setting:seeded-access-control",
        "store:redis",
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("twilio", "whatsapp", "llm")),
        reason="the bridge stubs are the mock leg; real legs run on the creds host",
    ),
]


async def test_a_message_delivered_twice_at_once_runs_one_turn(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = uniq("dedupe-site").replace("_", "-")
    route_name = uniq("dedupe-route").replace("_", "-")
    exec_key = uniq("dedupe-exec")
    marker = uniq("dedupe")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_extras_probe",
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        start_expr=f'{{marker: "{marker}"}}',
        reply_expr='"ok"',
    )
    web, page = await WebChatClient.open_page(
        bridge.stack.origin(bridge.stack.port_b), identity, store_url=bridge.stack.resources.redis_url
    )
    assert page.status_code == 200, page.text

    retry_key = secrets.token_urlsafe(16)
    first, second = await asyncio.gather(
        web.send("only once", client_message_id=retry_key), web.send("only once", client_message_id=retry_key)
    )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    message_id = first.json()["data"]["message_id"]
    assert second.json()["data"]["message_id"] == message_id

    # A turn runs only behind its own record, so the thread holding the one record — answered —
    # and the one probe entry are the whole of what the two deliveries produced.
    async def answered() -> list | None:
        threads = await bridge.api().get(f"/api/conversations/{route_name}/threads")
        if len(threads["items"]) != 1:
            return None
        transcript = await bridge.api().get(
            f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': threads['items'][0]['thread_id']})}"
        )
        items = transcript["items"]
        return items if items and all(item.get("answer_status") == "answered" for item in items) else None

    items = await wait_for_async(answered, deadline=20.0, message="the turn never answered")
    assert len(bridge.stack.records(f"extras:{marker}")) == 1
    assert [item["message_id"] for item in items] == [message_id], items
