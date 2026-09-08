"""L28 — the poll-only api door (a ``door=api`` route that declares no callback).

An api route MAY omit ``callback_url``: created (no callback secret minted), a sent
message whose turn outruns the sync wait answers ``202 {message_id, thread_id}``, and the
answer is read back at the poll door (``GET .../messages/{message_id}``) once the turn
completes. Its delivery record ends terminal ``delivered`` with NO callback attempt spent
(``attempts == 0``) — nothing is ever POSTed. A route that DOES declare a callback still
requires an absolute https URL: a plain-http callback is refused ``400`` at create.

The scripted-llm leg (as L3): ``build_bridge_stack`` wires the bridge LLM env, so a real
``TAI_E2E_REAL=llm`` run sends the turn to the live provider and the scripting no longer
holds — the module steps aside there, inert in the default mock run.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e.settings import HarnessSettings

from ._bridge_support import BridgeHarness, script_reply, wait_record_status

pytestmark = pytest.mark.skipif(
    HarnessSettings().is_real("llm"),
    reason="scripted llm_stub is the 'llm' mock leg (bridge LLM env); the real leg on the creds host",
)

# A plain-http callback url — refused at create even now that the callback is optional.
_HTTP_CALLBACK = "http://127.0.0.1:9/callback"


async def _poll_only_route(bridge: BridgeHarness, uniq: Callable[[str], str]) -> str:
    """Create a ``door=api`` route with NO ``callback_url`` and return its name. Asserts the
    create succeeds and mints no callback secret (the poll-only shape)."""
    route_name = uniq("l28-route").replace("_", "-")
    execution_key = uniq("l28-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    created = await bridge.api().post(
        f"/api/conversations/{route_name}",
        json={
            "door": "api",
            "target_kind": "agent",
            "target_name": "tools_agent",
            "execution_key": execution_key,
        },
    )
    assert created["created"] is True
    # A poll-only api row mints no callback secret (there is no callback to sign).
    assert created["callback_secret"] is None
    return route_name


async def test_poll_only_answer_read_at_message_door(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    route_name = await _poll_only_route(bridge, uniq)
    answer = uniq("l28-poll")
    script_reply(bridge.llm_stub, f"poll {answer}")

    caller_token = await bridge.mint_key(user_id=uniq("l28-caller"), scopes=["e2e-all"])
    # No ``wait_seconds`` (a 0-second wait the turn always outruns): the door answers 202
    # with the ids and no inline answer, and never suppresses a callback (there is none).
    accepted = await bridge.api(token=caller_token).post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": uniq("l28-user"), "text": "hello"},
        expect=202,
    )
    message_id = accepted["message_id"]
    assert accepted["thread_id"]
    assert "answer" not in accepted

    # The turn completes and its record lands terminal ``delivered`` — nothing POSTed, so no
    # send attempt is ever spent (a callback delivery would bump ``attempts``).
    record = await wait_record_status(bridge, route_name, message_id, {"delivered"}, deadline=20.0)
    assert record["answer_status"] == "answered"
    assert answer in record["answer"]
    assert record["attempts"] == 0

    # The caller reads the same answer back at the poll door — the sink a route with no
    # callback serves its answer through.
    caller_read = await bridge.api(token=caller_token).get(f"/api/conversations/{route_name}/messages/{message_id}")
    assert answer in caller_read["answer"]
    assert caller_read["delivery_status"] == "delivered"


async def test_http_callback_still_refused(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    route_name = uniq("l28-http").replace("_", "-")
    execution_key = uniq("l28-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    # A declared callback is still held to an absolute https url: a plain-http one is a 400.
    resp = await bridge.api().request_raw(
        "POST",
        f"/api/conversations/{route_name}",
        json={
            "door": "api",
            "target_kind": "agent",
            "target_name": "tools_agent",
            "execution_key": execution_key,
            "callback_url": _HTTP_CALLBACK,
        },
    )
    assert resp.status_code == 400, resp.text
