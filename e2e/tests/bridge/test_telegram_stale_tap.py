"""A telegram button tap on a keyboard no live question holds reaches the route as the user's message.

The tap answers nothing, so it never touches an ask: the channel reads the tapped button's label off
the keyboard Telegram echoes with the tap and bridges it as a message from the user, a reply to the
keyboard's message. The user reads the route's reply to that message, never the choice-unavailable notice.

Driven over the live media-bridge stack; the channel mocks over its in-process stub, so any real
selection breaks the stub and the module steps aside.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    TELEGRAM_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    wait_channel_send_count,
    wait_probe_record,
)
from ._rejection_support import REJECTION_NOTICES, notices_to

pytestmark = [
    pytest.mark.needs(
        "kind:identity", "probe-tools", "setting:conversations", "setting:seeded-access-control", "store:redis"
    ),
    pytest.mark.skipif(
        any(HarnessSettings().is_real(seam) for seam in ("telegram", "slack", "twilio", "whatsapp", "llm")),
        reason="the media-bridge stubs are the mock leg; real legs run under TAI_E2E_REAL",
    ),
]


@pytest.mark.needs("kind:channels:telegram", "helper:telegram")
async def test_a_tap_no_live_question_holds_is_bridged_as_the_users_message_with_the_button_label(
    media_bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    chat_id, message_id = "980001", 98001
    label, token = uniq("tg-stale-label"), uniq("tg-stale-token")
    keyboard = [[{"text": uniq("tg-other-label"), "callback_data": uniq("tg-other-token")}]]
    keyboard.append([{"text": label, "callback_data": token}])

    probe, reply = uniq("tg-stale-probe"), uniq("tg-stale-reply")
    route_name = uniq("tg-stale-route").replace("_", "-")
    execution_key = uniq("tg-stale-exec")
    await media_bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await media_bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_record",
        execution_key=execution_key,
        channel="telegram",
        our_identity=media_bridge.telegram_our_identity,
        start_expr=f'{{key: "{probe}", value: ({{message: .message, reply_to: .reply_to.message_id}} | tojson)}}',
        reply_expr=f'"{reply}"',
    )
    try:
        tap = media_bridge.telegram_callback_query(
            chat_id=chat_id, message_id=message_id, data=token, keyboard=keyboard
        )
        resp = await post_inbound(media_bridge.stack, TELEGRAM_INBOUND_PATH, tap, port=media_bridge.stack.port_b)
        assert resp.status_code in (200, 204), resp.text

        (turn,) = await wait_probe_record(media_bridge, probe)
        assert json.loads(turn["value"]) == {"message": label, "reply_to": str(message_id)}
        await wait_channel_send_count(media_bridge.fake_telegram, reply, 1)
    finally:
        await media_bridge.api().delete(f"/api/conversations/{route_name}", expect=200)

    unavailable = notices_to(media_bridge.fake_telegram, REJECTION_NOTICES["choice_unavailable"], chat_id, "chat_id")
    assert unavailable == [], f"a bridged stale tap must not send the choice-unavailable notice: {unavailable!r}"
