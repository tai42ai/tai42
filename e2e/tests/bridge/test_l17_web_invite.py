"""L17 — the web invite: a pair code carried into the web chat by the ``?tai_pair=`` URL.

One AGENT target reachable on a channel route (twilio) and a web route. The person mints a
code on the channel side (``/link``), then opens the web chat at ``?tai_pair=<code>``. The chat
page carries the code as a browser-side coordinate the bundle submits ONCE as the visitor's
first message — reusing the ordinary message door and the whole intercept path, with no new
redemption door. That single submit redeems into a merge and the visitor is answered
``linked`` in their own transcript.

The single-submit contract is the point of stripping the URL after the send: the code is
single-use, so a page that submitted it twice — or a reload that resubmitted the still-present
query — would redeem nothing the second time. This leg drives the door the bundle drives
(the harness runs no browser, so it stands in for the one submit), and pins that a second
submit of the same code is refused: exactly one redemption per invite.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable

import pytest
from _bridge_support import (
    INVALID_CODE_TEXT,
    LINK_REPLY_PREFIX,
    LINKED_TEXT,
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    cancel_and_join,
    extract_pair_code,
    post_inbound,
    wait_send_to,
)

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT, BRIDGE_TWILIO_FROM
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.webchat import WebChatClient

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio"),
    reason="FakeTwilio is the 'twilio' mock leg; the real leg runs on the creds host",
)

_AGENT = "tools_agent"


def _outbound(text: str) -> Callable[[str, dict], bool]:
    """Predicate for the agent-side answer arriving on the visitor's live stream."""
    return lambda event, data: event == "chat.message" and data["direction"] == "out" and text in data["text"]


async def _await_web_out(web: WebChatClient, submit: Callable[[], Awaitable[object]], needle: str) -> None:
    """Submit on the web door while a stream reader waits for an outbound frame carrying
    ``needle``, exactly as L12 does — the answer is caught whichever side of the stream open
    it lands on."""
    tail = asyncio.create_task(web.frames(until=_outbound(needle), deadline=40.0))
    try:
        sent = await submit()
        assert getattr(sent, "status_code", 200) == 200, getattr(sent, "text", sent)
        await asyncio.wait_for(tail, timeout=45.0)
    finally:
        await cancel_and_join(tail)


async def test_a_pair_url_invite_redeems_on_the_first_and_only_submit(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port = bridge.stack.port_b
    await bridge.set_target_config(target_kind="agent", target_name=_AGENT, multichannel=True)

    # The channel side of the same target: mint a code with /link over twilio.
    exec_ch = uniq("l17-exec-ch")
    await bridge.mint_key(user_id=exec_ch, scopes=["e2e-all"])
    route_ch = uniq("l17-route-ch").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_ch, agent=_AGENT, execution_key=exec_ch, channel="twilio", our_identity=BRIDGE_TWILIO_FROM
    )
    link_inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="/link", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, link_inbound, port=port)).status_code == 204
    (link_reply,) = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, needle=LINK_REPLY_PREFIX)
    code = extract_pair_code(link_reply["body"])

    # The web side of the same target: a web route on its own identity.
    identity = uniq("l17-site").replace("_", "-")
    route_web = uniq("l17-route-web").replace("_", "-")
    exec_web = uniq("l17-exec-web")
    await bridge.mint_key(user_id=exec_web, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_web, agent=_AGENT, execution_key=exec_web, channel="web", our_identity=identity
    )

    # Open the chat page at ?tai_pair=<code>. The server ignores the query, so the shell it serves
    # is the ordinary one (the code is the browser's coordinate), and the session it mints is
    # this visitor's conversation.
    base_url = f"http://{bridge.stack.host}:{bridge.stack.port_b}"
    web, page = await WebChatClient.open_page(
        base_url, identity, store_url=bridge.stack.resources.redis_url, query={"tai_pair": code}
    )
    assert page.status_code == 200, page.text
    assert f'data-identity="{identity}"' in page.text
    assert re.search(r'src="/api/channels/web/assets/([^"]+)"', page.text) is not None, page.text[:1000]

    # Stand in for the bundle's ONE submit of the ?tai_pair code as the visitor's first message —
    # it redeems into a merge and the visitor is answered ``linked`` in their own transcript.
    await _await_web_out(web, lambda: web.send(code), LINKED_TEXT)

    # Single-use: a SECOND submit of the same code (a page that resubmitted, or a reload that
    # kept the query) redeems nothing — it is answered the uniform refusal. Exactly one
    # redemption per invite, which is why the page strips the URL after the first submit.
    await _await_web_out(web, lambda: web.send(code), INVALID_CODE_TEXT)

    # The replay of the visitor's transcript is exactly: the two submitted codes and their two
    # answers, in order — one linked, one refused — and nothing else.
    replayed = await web.frames()
    exchange = [(data["direction"], data["text"]) for event, data in replayed if event == "chat.message"]
    assert [direction for direction, _text in exchange] == ["in", "out", "in", "out"]
    assert LINKED_TEXT in exchange[1][1]
    assert INVALID_CODE_TEXT in exchange[3][1]
