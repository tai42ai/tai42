"""L15 — the pairing guard rails: OFF passthrough, the uniform refusal, and redelivery.

Three invariants the intercept must hold, none of which is a happy-path link:

- On a target with multichannel OFF, ``/link`` and a pair code are ORDINARY TEXT — they
  route to the target untouched, byte-identical to a deployment that never heard of pairing.
  Read through an echo tool: what comes back is exactly what was sent.

- On a multichannel target, an unknown / malformed / already-spent code answers ONE uniform
  refusal — no oracle distinguishing "never existed" from "expired" from "used".

- A REDELIVERED redeem webhook (the provider re-POSTs the same message id) has NO second
  effect: idempotency on ``(channel, provider_message_id)`` returns the first turn's id and
  starts no second turn, so the code is judged exactly once and exactly one reply is sent.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
    BRIDGE_WHATSAPP_PHONE_ID_B,
)
from tai42_e2e.settings import HarnessSettings

from ._bridge_support import (
    INVALID_CODE_TEXT,
    LINK_REPLY_PREFIX,
    TWILIO_INBOUND_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    wait_send_to,
)

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm") or HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp"),
    reason="scripted-LLM + channel stubs are the 'llm'/'twilio'/'whatsapp' mock leg; real on creds host",
)

_OFF_TOOL = "e2e_echo"
_ON_AGENT = "tools_agent"
_REDELIV_CLIENT = "15559004444"


async def test_off_passthrough_uniform_refusal_and_redelivery(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port = bridge.stack.port_b

    # --- multichannel OFF: /link and a code are plain text (no config row at all) ----------
    exec_off = uniq("l15-exec-off")
    await bridge.mint_key(user_id=exec_off, scopes=["e2e-all"])
    route_off = uniq("l15-route-off").replace("_", "-")
    await bridge.create_tool_channel_route(
        route_name=route_off,
        tool=_OFF_TOOL,
        execution_key=exec_off,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        payload_expr="{payload: .message}",
    )
    # ``/link`` reaches the echo tool verbatim — it is NOT intercepted into a mint.
    link_inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text="/link", port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, link_inbound, port=port)).status_code == 204
    (echoed_link,) = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, needle="/link")
    assert echoed_link["body"] == "/link"
    # A well-formed code reaches the tool verbatim too — it is NOT redeemed.
    plain_code = "LINK-PASSTHRU"  # LINK- + 8 [A-Z0-9]
    code_inbound = bridge.twilio_inbound(
        our_identity=BRIDGE_TWILIO_FROM, client=BRIDGE_TWILIO_CLIENT, text=plain_code, port=port
    )
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, code_inbound, port=port)).status_code == 204
    (echoed_code,) = await wait_send_to(bridge.fake_twilio, to=BRIDGE_TWILIO_CLIENT, needle=plain_code)
    assert echoed_code["body"] == plain_code
    # Nothing on the OFF target ever produced a mint or a refusal — pairing was inert here.
    assert bridge.fake_twilio.sends_matching(LINK_REPLY_PREFIX) == []
    assert bridge.fake_twilio.sends_matching(INVALID_CODE_TEXT) == []

    # --- multichannel ON: an unknown code answers the ONE uniform refusal ------------------
    await bridge.set_target_config(target_kind="agent", target_name=_ON_AGENT, multichannel=True)
    exec_err = uniq("l15-exec-err")
    await bridge.mint_key(user_id=exec_err, scopes=["e2e-all"])
    route_err = uniq("l15-route-err").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_err,
        agent=_ON_AGENT,
        execution_key=exec_err,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID,
    )
    unknown = "LINK-UNKNOWN1"  # LINK- + 8 [A-Z0-9]; never minted, so redeem fails
    bad_inbound = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID, wa_id=BRIDGE_WHATSAPP_CLIENT, text=unknown
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, bad_inbound, port=port)).status_code == 200
    (refusal,) = await wait_send_to(bridge.fake_whatsapp, to=BRIDGE_WHATSAPP_CLIENT, needle=INVALID_CODE_TEXT)
    assert refusal["body"] == INVALID_CODE_TEXT

    # --- a redelivered redeem webhook has NO second effect ---------------------------------
    exec_re = uniq("l15-exec-re")
    await bridge.mint_key(user_id=exec_re, scopes=["e2e-all"])
    route_re = uniq("l15-route-re").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_re,
        agent=_ON_AGENT,
        execution_key=exec_re,
        channel="whatsapp",
        our_identity=BRIDGE_WHATSAPP_PHONE_ID_B,
    )
    replayed = "LINK-REPLAY12"  # LINK- + 8 [A-Z0-9]
    wamid = uniq("l15-wamid")
    first = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID_B, wa_id=_REDELIV_CLIENT, text=replayed, wamid=wamid
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, first, port=port)).status_code == 200
    (first_refusal,) = await wait_send_to(bridge.fake_whatsapp, to=_REDELIV_CLIENT, needle=INVALID_CODE_TEXT)
    assert first_refusal["body"] == INVALID_CODE_TEXT

    # The SAME provider message id, redelivered: caught by inbound idempotency before any
    # turn, so no second redeem is judged and no second reply is ever sent.
    redelivered = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID_B, wa_id=_REDELIV_CLIENT, text=replayed, wamid=wamid
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, redelivered, port=port)).status_code == 200

    # A settled presence sentinel ordered AFTER the redelivery ON THE SAME THREAD: a plain
    # passthrough message from _REDELIV_CLIENT on the same route runs a whole agent turn and
    # delivers its scripted answer. Per-thread FIFO is keyed by (route, client_address), so
    # this barrier shares the redelivery's thread — it is dispatched after the redelivery and
    # cannot complete until the redelivery has. Any errant second redeem turn the redelivery
    # might have scheduled would therefore have landed its refusal before the barrier's reply
    # arrives, so now absence means absence. Two subtleties force this exact shape: a barrier on
    # a DIFFERENT client runs on a DIFFERENT thread and gives no such ordering, and waiting on a
    # refusal COUNT could settle early on [first, errant] before the barrier landed — so the
    # barrier must be a same-thread reply matched by a UNIQUE body, not a refusal tally.
    barrier_answer = uniq("l15-barrier-ans")
    script_reply(bridge.llm_stub, barrier_answer)
    barrier = bridge.whatsapp_inbound(
        phone_number_id=BRIDGE_WHATSAPP_PHONE_ID_B,
        wa_id=_REDELIV_CLIENT,
        text=uniq("l15-barrier"),  # plain, non-code, non-command text → passthrough to the agent
        wamid=uniq("l15-wamid-b"),
    )
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, barrier, port=port)).status_code == 200
    await wait_send_to(bridge.fake_whatsapp, to=_REDELIV_CLIENT, needle=barrier_answer)
    to_redeliv_client = [
        r for r in bridge.fake_whatsapp.sends_matching(INVALID_CODE_TEXT) if r["to"] == _REDELIV_CLIENT
    ]
    assert len(to_redeliv_client) == 1, f"a redelivered redeem webhook produced a second effect: {to_redeliv_client!r}"
