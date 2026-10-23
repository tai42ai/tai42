"""A telegram send to an ``@username`` is recorded under the numeric chat Telegram resolved it to.

The Bot API answers every send with the numeric ``chat.id`` it went to, whatever the caller named,
and the inbound door names a participant by that same numeric id. So a ``notify_user`` to an
allowlisted ``@username`` starts the thread of the numeric chat, a message from that chat continues
it, and the message-ref read for the numeric address and the sent message id finds the send record.
The message-ref read has no HTTP door, so the leg reads its key from the stack's Redis.
"""

from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlencode

import pytest
import redis
from tai42_skeleton.conversations.settings import ConversationsSettings

from tai42_e2e.manifests import TELEGRAM_ALLOWED_HANDLE
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import (
    TELEGRAM_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    tool_text,
    wait_channel_send_count,
)

pytestmark = [
    pytest.mark.needs(
        "kind:channels:telegram",
        "kind:identity",
        "helper:llm",
        "helper:telegram",
        "setting:agent",
        "setting:conversations",
        "setting:seeded-access-control",
        "setting:router:notifications",
        "setting:tool:notify_user",
        "store:redis",
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("telegram", "llm")),
        reason="FakeTelegram + scripted-LLM are the 'telegram'/'llm' mock leg",
    ),
]


def _transcript_path(route_name: str, thread_id: str) -> str:
    return f"/api/conversations/{route_name}/transcript?{urlencode({'thread_id': thread_id, 'order': 'asc'})}"


async def test_a_notify_to_a_handle_is_recorded_under_the_numeric_chat(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    bridge = media_bridge
    fake = bridge.telegram
    identity = bridge.telegram_our_identity
    route_name = uniq("srtg-route").replace("_", "-")
    execution_key = uniq("srtg-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=execution_key,
        channel="telegram",
        our_identity=identity,
    )
    notice = uniq("srtg-notice")

    async with bridge.stack.mcp(port=bridge.stack.port_a, auth=bridge.root_token) as mcp:
        result = await mcp.call_tool(
            "notify_user", {"message": notice, "channel": "telegram", "recipient": TELEGRAM_ALLOWED_HANDLE}
        )
    assert "notification sent via 'telegram'" in tool_text(result)
    (sent,) = await wait_channel_send_count(fake, notice, 1)
    assert sent["chat_id"] == TELEGRAM_ALLOWED_HANDLE
    chat = str(sent["result_chat_id"])
    assert chat.lstrip("-").isdigit(), chat
    thread_id = f"bridge:{route_name}:{chat}"

    (listed,) = (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["items"]
    assert (listed["thread_id"], listed["client_address"]) == (thread_id, chat)
    (send,) = (await bridge.api().get(_transcript_path(route_name, thread_id)))["items"]
    assert (send["origin"], send["sent_by"], send["answer"]) == ("send", "notify", notice)
    assert send["sends"] == [{"part": 0, "chars": len(notice), "provider_message_ids": [str(sent["message_id"])]}]

    answer = uniq("srtg-answer")
    script_reply(bridge.llm_stub, answer)
    inbound = bridge.telegram_inbound_text(chat_id=chat, text="thanks")
    assert (await post_inbound(bridge.stack, TELEGRAM_INBOUND_PATH, inbound)).status_code == 200
    await wait_channel_send_count(fake, answer, 1)

    async def continued() -> list[dict] | None:
        items = (await bridge.api().get(_transcript_path(route_name, thread_id)))["items"]
        return items if len(items) == 2 and items[1]["answer"] == answer else None

    first, turn = await wait_for_async(continued, deadline=15.0, message="the chat's message never continued it")
    assert first["message_id"] == send["message_id"]
    assert (turn["origin"], turn["client_address"]) == ("client", chat)
    assert (await bridge.api().get(f"/api/conversations/{route_name}/threads"))["total"] == 1

    settings = ConversationsSettings(prefix=bridge.stack.config.env["CONVERSATIONS_PREFIX"])
    ref_key = settings.message_ref_key("telegram", identity, chat, str(sent["message_id"]))
    client = redis.Redis.from_url(bridge.stack.resources.redis_url, decode_responses=True)
    try:
        assert client.lrange(ref_key, 0, -1) == [send["message_id"]]
    finally:
        client.close()
