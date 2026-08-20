"""L26 — a cross-worker contended thread lease refuses a bounded sync door with 503.

A conversation turn holds a token-fenced Redis lease keyed on its ``thread_id`` for its whole
span (``TurnCaps.run_reserved``). A background/channel turn acquires that lease UNBOUNDED; a
live-caller sync door on a DIFFERENT worker that contends for the same thread bounds its whole
acquisition by ``sync_door_wait_seconds``, and a wait past the bound raises ``ThreadBusyError``
— a loud, retriable 503 (``UnavailableError``) rather than a block past the proxy timeout.

The two replicas share ONE conversations Redis, so a lease worker A holds is visible to worker
B. A channel turn is driven on ``port_a`` and held open inside its model call (the scripted LLM
stub sleeps in the completion, holding the turn — and its lease — open); while it holds, an
operator send on ``port_b`` for the SAME thread waits out its bound and returns the busy 503.
Once the held turn completes and the lease releases, the SAME door on ``port_b`` succeeds — so
the bound is proven to RELEASE, not merely to reject.

The busy 503 is disambiguated from the harness's boot/reload-gate 503 (``{"reloading": true}``):
the raw body must carry the ``ThreadBusyError`` ``{"error": ...}`` shape, so the test can never
pass on a reload-gate 503 by accident.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable

import pytest

from tai42_e2e.manifests import BRIDGE_SYNC_DOOR_WAIT_SECONDS, BRIDGE_TWILIO_CLIENT
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import (
    TWILIO_INBOUND_PATH,
    BridgeHarness,
    post_inbound,
    script_reply,
    wait_send_to,
)

# The scripted LLM turns are the 'llm' mock leg; the twilio signed inbound the 'twilio' one.
pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm") or HarnessSettings().is_real("twilio"),
    reason="scripted-LLM + twilio stub are the 'llm'/'twilio' mock leg; real on the creds host",
)

_AGENT = "tools_agent"

# The held turn's model call sleeps this long, comfortably above the sync-door acquire bound, so
# the contender's bounded wait expires deterministically while the turn still holds the lease.
_HELD_TURN_SECONDS = BRIDGE_SYNC_DOOR_WAIT_SECONDS + 6.0


def _thread_messages_path(route_name: str) -> str:
    """The operator-send door — a bounded live-caller sync door contending for the thread's slot."""
    return f"/api/conversations/{route_name}/thread/messages"


async def test_a_contended_thread_lease_refuses_a_bounded_sync_door_then_releases(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    port_a = bridge.stack.port_a
    port_b = bridge.stack.port_b
    route_name = uniq("l26-route").replace("_", "-")
    execution_key = uniq("l26-exec")
    identity = f"+1555{secrets.randbelow(10**7):07d}"
    client = BRIDGE_TWILIO_CLIENT
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_channel_route(
        route_name=route_name, agent=_AGENT, execution_key=execution_key, channel="twilio", our_identity=identity
    )

    held_answer = uniq("l26-held")
    operator_text = uniq("l26-operator")
    # One scripted turn — the held channel turn. The operator sends run NO model turn, so they
    # take none; an accidental extra completion would fault the stub loudly.
    script_reply(bridge.llm_stub, held_answer)

    # Hold the channel turn's model call open for the whole window so its lease stays held while
    # the contender on B waits out its bound.
    bridge.llm_stub.set_response_delay(_HELD_TURN_SECONDS)
    try:
        # 1. Drive the held turn on A. The door acks 204 once the intake record is written and
        #    the turn is scheduled; the turn task then acquires the lease and enters the model.
        inbound = bridge.twilio_inbound(our_identity=identity, client=client, text=uniq("l26-in"), port=port_a)
        assert (await post_inbound(bridge.stack, TWILIO_INBOUND_PATH, inbound, port=port_a)).status_code == 204

        # The lease is HELD once the turn reaches its model call: the scripted stub records the
        # completion request at the start of the (now sleeping) handler, and ``run_reserved``
        # has already taken the lease by the time the agent calls the model. This barrier is
        # what makes the contention deterministic — the contender fires only once A holds it.
        async def _turn_holds_lease() -> bool:
            return len(bridge.llm_stub.requests) >= 1

        await wait_for_async(
            _turn_holds_lease, deadline=15.0, message="the held turn never reached its model call to hold the lease"
        )

        # The thread is real and route-keyed once the intake record is written; read its id off
        # the listing (the contended door validates the thread belongs to the route).
        async def _thread_id() -> str | None:
            listing = await bridge.api(port=port_b).get(f"/api/conversations/{route_name}/threads")
            items = listing["items"]
            return items[0]["thread_id"] if items else None

        thread_id = await wait_for_async(_thread_id, deadline=10.0, message="the held turn's thread never listed on B")

        # 2. Contend on B: a bounded operator send for the SAME thread waits out its
        #    ``sync_door_wait_seconds`` behind A's lease and refuses with the busy 503.
        contended = await bridge.api(port=port_b).request_raw(
            "POST", _thread_messages_path(route_name), json={"thread_id": thread_id, "text": operator_text}
        )
        assert contended.status_code == 503, contended.text
        body = contended.json()
        # It is the ThreadBusyError 503, NOT the harness's reload-gate 503 — so this can never
        # pass on a boot-gate rejection by accident.
        assert "reloading" not in body, body
        assert "is busy with an in-flight turn" in body.get("error", ""), body

        # 3. The held turn completes and delivers its answer — the lease is now released.
        await wait_send_to(bridge.fake_twilio, to=client, needle=held_answer, deadline=_HELD_TURN_SECONDS + 20.0)

        # 4. Happy path: the SAME door on B now acquires the freed slot and succeeds (2xx), and
        #    its by-hand message is delivered — the bound RELEASES, it does not merely reject.
        result = await bridge.api(port=port_b).post(
            _thread_messages_path(route_name), json={"thread_id": thread_id, "text": operator_text}
        )
        assert result["thread_id"] == thread_id
        await wait_send_to(bridge.fake_twilio, to=client, needle=operator_text, deadline=20.0)
    finally:
        bridge.llm_stub.set_response_delay(0.0)
