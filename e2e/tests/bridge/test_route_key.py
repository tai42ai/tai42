"""Route key: the name of the route a turn entered through, carried on the tool payload.

Every inbound turn payload carries ``route`` — the name of the conversation route the turn
entered through — beside ``our_identity`` and ``channel``. It is a required non-blank string
on every door, so a flow bound to several routes can tell them apart from the payload alone.
This leg drives both doors against ``target_kind=tool`` routes whose ``start_expr`` reads
``.route`` and records what the tool actually received, so the assertion is on the delivered
payload and not on any intermediate.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from tai42_e2e.waiting import wait_for_async
from tai42_e2e.webchat import WebChatClient

from ._bridge_support import BridgeHarness, wait_probe_entries

# An https callback with no server behind it — the api-door tool legs map their reply to null
# and wait inline, so the callback is suppressed and never dialled; it names no reachable host.
_UNREACHABLE_CALLBACK = "https://127.0.0.1:9/callback"


def _record_expr(key_expr: str, value_expr: str) -> str:
    """A jq program mapping the turn payload to ``e2e_record(key, value)`` kwargs — ``key`` and
    ``value`` are jq snippets (a quoted literal, or a path like ``.route``)."""
    return f"{{key: {key_expr}, value: {value_expr}}}"


def _base_url(bridge: BridgeHarness) -> str:
    return bridge.stack.origin(bridge.stack.port_b)


async def _wait_record_count(bridge: BridgeHarness, key: str, count: int, *, deadline: float = 20.0) -> list[dict]:
    """Wait until the ``e2e_record`` list under ``key`` holds ``count`` entries; assert it
    settled at exactly ``count`` and return them decoded."""

    async def probe() -> list[dict] | None:
        records = bridge.stack.records(key)
        return [json.loads(raw) for raw in records] if len(records) >= count else None

    entries = await wait_for_async(probe, deadline=deadline, message=f"fewer than {count} records under {key!r}")
    assert len(entries) == count, f"expected exactly {count} records under {key!r}, saw {len(entries)}"
    return entries


pytestmark = pytest.mark.needs("kind:identity", "probe-tools", "setting:conversations", "setting:seeded-access-control")


@pytest.mark.needs("kind:channels:web", "store:redis")
async def test_channel_route_start_expr_reads_its_own_route_name(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """A web channel route's ``start_expr`` reads ``.route`` and it is the route's own name — the
    key that lets a flow bound to several channel routes tell them apart."""
    probe = uniq("rk-ch")
    route_name = uniq("rk-ch-route").replace("_", "-")
    identity = uniq("rk-ch-site").replace("_", "-")
    exec_key = uniq("rk-ch-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_record",
        execution_key=exec_key,
        channel="web",
        our_identity=identity,
        start_expr=_record_expr(json.dumps(probe), ".route"),
        reply_expr="null",
    )

    web, _page = await WebChatClient.open_page(_base_url(bridge), identity, store_url=bridge.stack.resources.redis_url)
    assert (await web.send(uniq("rk-ch-msg"))).status_code == 200

    (entry,) = await _wait_record_count(bridge, probe, 1)
    assert entry["value"] == route_name


@pytest.mark.needs("store:redis")
async def test_api_routes_sharing_a_tool_are_told_apart_by_the_route_key(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    """Two api routes bound to the SAME tool each deliver their own name under ``.route``, so one
    flow fronting several api routes tells them apart from the payload alone."""
    probe = uniq("rk-api")
    exec_key = uniq("rk-api-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    caller = await bridge.mint_key(user_id=uniq("rk-api-caller"), scopes=["e2e-all"])

    route_a = uniq("rk-api-a").replace("_", "-")
    route_b = uniq("rk-api-b").replace("_", "-")
    for route_name in (route_a, route_b):
        await bridge.create_tool_api_route(
            route_name=route_name,
            tool="e2e_record",
            execution_key=exec_key,
            callback_url=_UNREACHABLE_CALLBACK,
            start_expr=_record_expr(json.dumps(probe), ".route"),
            reply_expr="null",
        )

    for route_name in (route_a, route_b):
        await bridge.api(token=caller).post(
            f"/api/conversations/{route_name}/messages",
            json={"external_user_id": uniq("rk-api-user"), "text": "ping", "wait_seconds": 20},
            expect=200,
        )

    recorded = await wait_probe_entries(bridge, probe, 2)
    assert sorted(entry["value"] for entry in recorded) == sorted((route_a, route_b))
