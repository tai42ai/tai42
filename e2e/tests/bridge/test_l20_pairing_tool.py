"""L20 — the ``get_pairing_code`` builtin as a tool-target route: the R8 contract end to end.

A deployment opts the ``get_pairing_code`` builtin in through a manifest ``tools[].module``
row (done on the bridge profile), then wires it as a tool-target route: the inbound payload
maps to the tool's ``(channel, our_identity, sender)`` via ``payload_expr``, and the tool
result — ``{code, expires_at}`` and nothing else (R8) — maps to the reply via ``reply_expr``.
No agent, no ``ask_user``: the tool runs directly under the route's execution key and its
return is delivered over the channel.

The leg pins the whole contract: a route extracting ``.code`` delivers a well-formed pair
code, a second route extracting ``.expires_at`` delivers an ISO-8601 future expiry, and the
delivered code is GENUINE — redeeming it from another channel links the two conversations. The
operator composes any invite wording around these two fields; the platform ships neither link
nor phrasing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from _bridge_support import (
    LINKED_TEXT,
    PAIR_CODE_RE,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    wait_send_to,
)

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_CLIENT_B,
    BRIDGE_TWILIO_FROM,
    BRIDGE_TWILIO_FROM_B,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp"),
    reason="FakeTwilio + FakeWhatsApp are the 'twilio'/'whatsapp' mock leg; real on the creds host (PLAN_2 §F)",
)

_TOOL = "get_pairing_code"
# The inbound payload the tool turn exposes ({message, sender, our_identity, channel}) mapped
# to the tool's typed signature.
_PAYLOAD_EXPR = "{channel: .channel, our_identity: .our_identity, sender: .sender}"


async def _tool_route(
    bridge: BridgeHarness, uniq: Callable[[str], str], *, channel: str, our_identity: str, reply_expr: str
) -> str:
    tag = uniq("l20-route").replace("_", "-")
    exec_key = uniq("l20-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=tag,
        tool=_TOOL,
        execution_key=exec_key,
        channel=channel,
        our_identity=our_identity,
        payload_expr=_PAYLOAD_EXPR,
        reply_expr=reply_expr,
    )
    return tag


async def test_get_pairing_code_tool_route_mints_a_live_code(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    port = bridge.stack.port_b
    # The tool resolves the (channel, our_identity) route and refuses unless that target has
    # multichannel ON — and the target here IS this tool, so it opts itself in.
    await bridge.set_target_config(target_kind="tool", target_name=_TOOL, multichannel=True)

    await _tool_route(bridge, uniq, channel="twilio", our_identity=BRIDGE_TWILIO_FROM, reply_expr=".code")
    await _tool_route(bridge, uniq, channel="twilio", our_identity=BRIDGE_TWILIO_FROM_B, reply_expr=".expires_at")
    await _tool_route(bridge, uniq, channel="whatsapp", our_identity=BRIDGE_WHATSAPP_PHONE_ID, reply_expr=".code")

    # The ``.code`` route: an inbound runs the tool, which mints a code for THIS sender, and
    # the reply is the raw code delivered over twilio.
    ask_code = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="a code please", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, ask_code, port=port)).status_code == 204
    (code_reply,) = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, needle="LINK-")
    code = code_reply["body"]
    assert PAIR_CODE_RE.fullmatch(code), f"the tool's .code reply is not a bare pair code: {code!r}"

    # The ``.expires_at`` route: the tool's other contract field, delivered as an ISO-8601 UTC
    # future expiry.
    ask_exp = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM_B, client=BRIDGE_TWILIO_CLIENT_B, text="when", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, ask_exp, port=port)).status_code == 204
    (exp_reply,) = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT_B, needle="+00:00")
    expires_at = datetime.fromisoformat(exp_reply["body"])
    assert expires_at > datetime.now(UTC), f"expires_at is not in the future: {exp_reply['body']!r}"
    # No LINK code leaked into the expiry-only reply.
    assert not PAIR_CODE_RE.search(exp_reply["body"]), exp_reply["body"]

    # The delivered code is a GENUINE live pair code: redeeming it from whatsapp links the two.
    redeem = bridge.whatsapp_inbound(phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=code)
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, redeem, port=port)).status_code == 200
    (linked_reply,) = await wait_send_to(bridge.fake_whatsapp, to=BRIDGE_WHATSAPP_CLIENT, needle=LINKED_TEXT)
    assert linked_reply["body"] == LINKED_TEXT
