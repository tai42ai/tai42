"""L3 — API-caller round-trip.

The authed ``POST /api/conversations/{route}/messages`` door: the sync-wait carrying the
answer inline in a ``200`` (callback suppressed), the async ``202`` whose permanently
undeliverable callback lands ``failed`` on the admin list door, and read-door scoping
(a second integration's key cannot read the first's record).

The signed-callback SUCCESS delivery is not exercised here: the contract forces an absolute
HTTPS ``callback_url`` with no insecure opt-out, so a plain-loopback receiver cannot stand in
without TLS. The sync-inline path and the failure path cover the door's outcomes without one.
"""

from __future__ import annotations

from collections.abc import Callable

from _bridge_support import BridgeHarness, script_reply, wait_record_status

# An https callback URL with no server behind it — the delivery POST connection-refuses, so
# the record exhausts its (shortened) attempts and lands terminal ``failed``.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


async def _api_route(bridge: BridgeHarness, uniq: Callable[[str], str], *, callback_url: str) -> str:
    route_name = uniq("l3-route").replace("_", "-")
    execution_key = uniq("l3-exec")
    await bridge.mint_key(user_id=execution_key, scopes=["e2e-all"])
    await bridge.create_api_route(
        route_name=route_name,
        agent="tools_agent",
        execution_key=execution_key,
        callback_url=callback_url,
    )
    return route_name


async def test_sync_wait_returns_answer_inline(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    route_name = await _api_route(bridge, uniq, callback_url=_UNREACHABLE_CALLBACK)
    answer = uniq("l3-sync")
    script_reply(bridge.llm_stub, f"sync {answer}")

    caller_token = await bridge.mint_key(user_id=uniq("l3-caller"), scopes=["e2e-all"])
    # A fast turn finishing inside the wait answers 200 with the answer inline; the callback
    # is suppressed (mark_wait_delivered) so it never double-fires.
    data = await bridge.api(token=caller_token).post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": uniq("l3-user"), "text": "hello", "wait_seconds": 20},
        expect=200,
    )
    assert data["answer"]["status"] == "answered"
    assert answer in data["answer"]["answer"]
    # The record settled delivered without a callback POST (the unreachable URL was never hit).
    record = await bridge.get_record(route_name, data["message_id"])
    assert record["delivery_status"] == "delivered"


async def test_async_callback_permanent_failure_marks_failed(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    route_name = await _api_route(bridge, uniq, callback_url=_UNREACHABLE_CALLBACK)
    answer = uniq("l3-async")
    script_reply(bridge.llm_stub, f"async {answer}")

    caller_token = await bridge.mint_key(user_id=uniq("l3-caller"), scopes=["e2e-all"])
    accepted = await bridge.api(token=caller_token).post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": uniq("l3-user"), "text": "hello"},
        expect=202,
    )
    message_id = accepted["message_id"]

    # The callback to the unreachable URL exhausts the shortened attempt ladder → failed.
    record = await wait_record_status(bridge, route_name, message_id, {"failed"}, deadline=20.0)
    assert record["answer_status"] == "answered"

    # The admin failed-delivery listing surfaces it.
    failed = await bridge.list_failed()
    assert any(item["message_id"] == message_id for item in failed["items"])


async def test_read_door_scoping(bridge: BridgeHarness, uniq: Callable[[str], str]) -> None:
    route_name = await _api_route(bridge, uniq, callback_url=_UNREACHABLE_CALLBACK)
    answer = uniq("l3-scope")
    script_reply(bridge.llm_stub, f"scoped {answer}")

    caller_one_id = uniq("l3-int-one")
    caller_one = await bridge.mint_key(user_id=caller_one_id, scopes=["e2e-all"])
    caller_two = await bridge.mint_key(user_id=uniq("l3-int-two"), scopes=["e2e-all"])

    data = await bridge.api(token=caller_one).post(
        f"/api/conversations/{route_name}/messages",
        json={"external_user_id": uniq("l3-user"), "text": "hello", "wait_seconds": 20},
        expect=200,
    )
    message_id = data["message_id"]

    # The invoking integration reads its own record (the caller-safe projection).
    own = await bridge.api(token=caller_one).get(f"/api/conversations/{route_name}/messages/{message_id}")
    assert own["caller_principal"] == caller_one_id
    assert "error" not in own  # the caller projection withholds the route-key run's detail

    # A SECOND integration's key cannot read the first's record — a 403, not the record.
    await bridge.api(token=caller_two).get(f"/api/conversations/{route_name}/messages/{message_id}", expect=403)

    # Admin (root) reads the full record including the internal bookkeeping.
    admin_view = await bridge.get_record(route_name, message_id)
    assert admin_view["caller_principal"] == caller_one_id
    assert "error" in admin_view
