"""L8 — restart durability + replay idempotency.

* A provider webhook replayed under the same message id starts NO second turn (the door's
  replay dedupe), while a genuinely new message on the same pair does.
* A record whose delivery already reached a terminal state survives a real serve-worker
  restart untouched: the boot re-drive respects it and never re-delivers it (exactly-once
  across a restart).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from _bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    default_twilio_identity,
    post_inbound,
    script_reply,
    wait_record_status,
    wait_twilio_send,
)

from tai42_e2e.manifests import BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings

# The scripted-LLM + FakeTwilio round-trips are the mock leg for BOTH the 'twilio' channel seam
# and the 'llm' seam (build_bridge_stack wires the LLM env too, so TAI_E2E_REAL=llm also sends
# the bridge LLM to the live provider). Either selection real breaks the stub scripting, so the
# module steps aside; the real legs run on the dedicated e2e creds host, not in CI.
# Inert in the default mock run — both is_real checks are False, so collection is byte-for-byte
# today's.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("twilio") or HarnessSettings().is_real("llm"),
    reason="FakeTwilio + scripted-LLM is the 'twilio'/'llm' mock leg; real legs on the creds host",
)

# An https callback URL with no server behind it — the delivery POST connection-refuses, so
# the record exhausts its (shortened) attempts and lands terminal ``failed``.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


async def test_provider_replay_starts_no_second_turn(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    identity = default_twilio_identity()
    client = BRIDGE_TWILIO_CLIENT
    exec_key = uniq("l8-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("l8-route").replace("_", "-")
    await bridge.create_channel_route(
        route_name=route_name, agent="tools_agent", execution_key=exec_key, channel="twilio", our_identity=identity
    )
    port = bridge.stack.port_b

    answer1 = uniq("l8-a1")
    answer2 = uniq("l8-a2")
    script_reply(bridge.llm_stub, f"first {answer1}", f"second {answer2}")

    # Fire 1 — one turn, one send.
    inbound = bridge.twilio_inbound(our_identity=identity, client=client, text="first question", port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204
    await wait_twilio_send(bridge.fake_twilio, answer1)
    assert len(bridge.llm_stub.requests) == 1

    # Replay the IDENTICAL webhook (same MessageSid) — the door dedupes it before any accept,
    # so no second turn runs and no second answer is sent.
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port)).status_code == 204
    assert len(bridge.llm_stub.requests) == 1
    assert len(bridge.fake_twilio.sends_matching(answer1)) == 1

    # A genuinely new message on the same pair (a fresh MessageSid) DOES start a turn.
    fresh = bridge.twilio_inbound(our_identity=identity, client=client, text="second question", port=port)
    assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, fresh, port=port)).status_code == 204
    await wait_twilio_send(bridge.fake_twilio, answer2)
    assert len(bridge.llm_stub.requests) == 2


async def test_terminal_record_survives_a_serve_restart_and_is_not_re_driven(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    exec_key = uniq("l8r-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("l8r-route").replace("_", "-")
    await bridge.create_api_route(
        route_name=route_name, agent="tools_agent", execution_key=exec_key, callback_url=_UNREACHABLE_CALLBACK
    )

    answer = uniq("l8r")
    script_reply(bridge.llm_stub, f"async {answer}")
    caller_token = await bridge.mint_key(user_id=uniq("l8r-caller"), scopes=["e2e-all"])
    accepted = await bridge.api(token=caller_token).post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": uniq("l8r-user"), "text": "hello"},
        expect=202,
    )
    message_id = accepted["message_id"]

    # The unreachable callback exhausts the shortened attempt ladder → terminal failed.
    record = await wait_record_status(bridge, route_name, message_id, {"failed"}, deadline=20.0)
    attempts_before = record["attempts"]

    # Restart the serve worker that ran the turn; its boot re-drive re-reads the record store.
    bridge.stack.restart("serve-b")

    # The terminal record survived the restart and the re-drive left it exactly as it was —
    # a terminal record is never re-delivered, so its attempt count did not move.
    after = await bridge.get_record(route_name, message_id)
    assert after["delivery_status"] == "failed"
    assert after["answer_status"] == "answered"
    assert after["attempts"] == attempts_before
