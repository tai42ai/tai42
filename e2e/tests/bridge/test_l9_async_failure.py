"""L9 — async-failure: a 2xx send then a ``failed`` delivery-status webhook flips the record.

A bridge answer is accepted by the provider (the send is captured), leaving the record
``provisional`` awaiting an out-of-band receipt; a genuinely-signed ``failed`` status webhook
then drives it terminal ``failed`` and it surfaces on the admin failed-delivery listing. Both
providers carry the same out-of-band receipt path.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _bridge_support import (
    TWILIO_INBOUND_PATH,
    TWILIO_STATUS_PATH,
    WHATSAPP_INBOUND_PATH,
    BridgeHarness,
    drive_status_to_failed,
    post_inbound,
    script_reply,
    wait_twilio_send,
    wait_whatsapp_send,
)

from tai42_e2e.manifests import (
    BRIDGE_TWILIO_CLIENT,
    BRIDGE_TWILIO_FROM,
    BRIDGE_WHATSAPP_CLIENT,
    BRIDGE_WHATSAPP_PHONE_ID,
)
from tai42_e2e.settings import HarnessSettings

# One test drives the scripted-LLM + FakeTwilio flow, the other the scripted-LLM + FakeWhatsApp
# flow, so this module is the mock leg for the 'twilio', 'whatsapp' AND 'llm' seams
# (build_bridge_stack wires the LLM env too, so TAI_E2E_REAL=llm also sends the bridge LLM to
# the live provider). Any of those selections real breaks the stub scripting, so the module
# steps aside; the real legs run on the dedicated e2e creds host, not in CI. Inert
# in the default mock run — every is_real check is False, so collection is byte-for-byte today's.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("whatsapp") or HarnessSettings().is_real("llm"),
    reason="channel stubs + scripted-LLM are the 'twilio'/'whatsapp'/'llm' mock leg; real on creds host",
)


async def test_twilio_failed_status_flips_the_record(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    identity = BRIDGE_TWILIO_FROM
    client = BRIDGE_TWILIO_CLIENT
    exec_key = uniq("l9-tw-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("l9-tw-route").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_name, agent="tools_agent", execution_key=exec_key, channel="twilio", our_identity=identity
    )
    port = bridge.stack.port_b

    answer = uniq("l9-tw")
    script_reply(bridge.llm_stub, f"tw {answer}")
    inbound = bridge.twilio_inbound(our_identity=identity, client=client, text="hello", port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204
    send = await wait_twilio_send(bridge.fake_twilio, answer)

    async def post_failed() -> object:
        status = bridge.twilio_status(message_sid=send["sid"], status="failed", port=port)
        return await post_inbound(bridge.stack, TWILIO_STATUS_PATH, status, port=port)

    item = await drive_status_to_failed(bridge, post_failed, send["sid"])
    assert item["delivery_status"] == "failed"
    # The turn itself answered; only its delivery failed.
    assert item["answer_status"] == "answered"
    assert item["channel"] == "twilio"


async def test_whatsapp_failed_status_flips_the_record(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    identity = BRIDGE_WHATSAPP_PHONE_ID
    client = BRIDGE_WHATSAPP_CLIENT
    exec_key = uniq("l9-wa-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("l9-wa-route").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=exec_key,
        channel="whatsapp",
        our_identity=identity,
    )
    port = bridge.stack.port_b

    answer = uniq("l9-wa")
    script_reply(bridge.llm_stub, f"wa {answer}")
    inbound = bridge.whatsapp_inbound(phone_number_id=identity, wa_id=client, text="hello")
    assert (await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, inbound, port=port)).status_code == 200
    send = await wait_whatsapp_send(bridge.fake_whatsapp, answer)

    async def post_failed() -> object:
        status = bridge.whatsapp_status(
            phone_number_id=identity, wamid=send["wamid"], status="failed", recipient_id=client
        )
        return await post_inbound(bridge.stack, WHATSAPP_INBOUND_PATH, status, port=port)

    item = await drive_status_to_failed(bridge, post_failed, send["wamid"])
    assert item["delivery_status"] == "failed"
    assert item["answer_status"] == "answered"
    assert item["channel"] == "whatsapp"
