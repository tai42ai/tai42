"""L25 — a web media-card option list, end to end over the visitor surface.

``notify_user(channel="web", options=[...])`` appends ONE ``chat.media`` card to the
visitor's transcript carrying the tappable options; a tap sends the option's own text
through the SAME public message door any typed message uses, so the option reaches the
turn as the visitor's message. The web channel is driven because its own public doors ARE
the medium — the visitor's SSE stream is the card surface, and the message door is where a
tap lands. The tool target records the delivered message, so the assertion is on what the
turn actually received.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from tai42_e2e.waiting import wait_for_async
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness


def _base_url(bridge: BridgeHarness) -> str:
    return f"http://{bridge.stack.host}:{bridge.stack.port_b}"


async def _web_record_route(bridge: BridgeHarness, uniq: Callable[[str], str], tag: str) -> str:
    """Create a ``target_kind=tool`` web route whose tool records the delivered message under
    its own text as the key, and return its identity. The reply maps to null so the turn runs
    (the record side effect lands) but nothing is appended to the visitor transcript."""
    identity = uniq(f"{tag}-site").replace("_", "-")
    route_name = uniq(f"{tag}-route").replace("_", "-")
    exec_key = uniq(f"{tag}-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_record",
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        payload_expr="{key: .message, value: .message}",
        reply_expr="null",
    )
    return identity


async def test_web_notify_options_card_and_a_tapped_option_lands_as_a_message(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    identity = await _web_record_route(bridge, uniq, "l25")
    web, page = await WebChatClient.open_page(_base_url(bridge), identity, store_url=bridge.stack.resources.redis_url)
    assert page.status_code == 200, page.text

    message = uniq("l25-list")
    option = uniq("l25-item")

    # The agent-initiated notify appends ONE tappable card to the visitor's stream. The
    # notify_user door names the visitor pair as the composite recipient, exactly as a
    # deliver does.
    await bridge.api().post(
        "/api/notifications",
        json={"message": message, "channel": "web", "recipient": web.recipient, "options": [option, "Item B"]},
    )

    def _is_card(event: str, data: dict) -> bool:
        return event == "chat.media" and data.get("text") == message

    frames = await web.frames(until=_is_card)
    card = next(data for event, data in frames if _is_card(event, data))
    assert card["options"] == [option, "Item B"]

    # A tap sends the option's own text through the message door, exactly as a typed message
    # would — so it reaches the turn as the visitor's message and the tool records it.
    sent = await web.send(option)
    assert sent.status_code == 200, sent.text

    async def _recorded() -> list[dict] | None:
        records = bridge.stack.records(option)
        return [json.loads(raw) for raw in records] if records else None

    entries = await wait_for_async(_recorded, deadline=20.0, message="the tapped option never reached the turn")
    assert any(entry["value"] == option for entry in entries)
