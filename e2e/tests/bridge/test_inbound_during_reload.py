"""A channel inbound that arrives while a worker reloads is refused before anything is written.

A turn door refuses during a config reload with the retriable ``503`` (``Retry-After: 5``,
``{"reloading": true}``) before any record, claim or vendor-side "seen" mark is written, so the
vendor's redelivery of the same message is processed in full: one turn, one reply.

The reload is forced through replica A's env write, which fans a reload out to replica B over the
worker bus. B's bus-driven reload drains its in-flight requests under its own reload gate before
the gate frees, so a request the test holds open on B keeps B's gate shut until the test releases
it: the window is closed by the test, never by the clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
from collections.abc import Callable
from urllib.parse import urlencode

import pytest

from tai42_e2e.httpapi import _is_reloading
from tai42_e2e.manifests import BRIDGE_TWILIO_FROM
from tai42_e2e.settings import HarnessSettings
from tai42_e2e.waiting import wait_for_async

from ._bridge_support import TWILIO_INBOUND_PATH, BridgeHarness, post_inbound, wait_probe_record, wait_twilio_send

# One fan-out reload has been measured at 13.6-25.9 s under load. The bound on one awaited fan-out
# reload is the derived 150 s (the 30 s bus apply timeout plus a 120 s boot-scale build), more than
# twice the slowest measured completion; it bounds a hang, it is not a throughput budget.
_RELOAD_BOUND_SECONDS = 150.0
# The env write's response waits for the fleet's acks, which B gives only after the test
# releases its held request: a client read timeout above the hold's 300 s cap plus margin.
_FLEET_WRITE_TIMEOUT_SECONDS = 330.0

pytestmark = [
    pytest.mark.needs(
        "kind:channels:twilio",
        "helper:twilio",
        "kind:identity",
        "probe-tools",
        "setting:conversations",
        "setting:seeded-access-control",
        "store:redis",
        "topology:replicas",
        "mutable",
    ),
    pytest.mark.skipif(
        HarnessSettings().is_real("twilio"),
        reason="FakeTwilio is the 'twilio' mock leg; the real leg runs under TAI_E2E_REAL",
    ),
]


async def _cancel_and_join(task: asyncio.Task) -> None:
    if task.done():
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


# The module fixture's boot (<= 120 s) plus one awaited fan-out reload (<= 150 s, the lock wait runs
# inside it) plus the test's own bounded waits (hold start 30 + probe record 12 + twilio send 12 +
# answered 20 = 74 s): 344 s, rounded up to 360 s.
@pytest.mark.timeout(360)
async def test_a_channel_inbound_during_a_reload_is_refused_and_its_redelivery_runs_once(
    bridge: BridgeHarness, uniq: Callable[[str], str]
) -> None:
    stack = bridge.stack
    api_a = bridge.api(port=stack.port_a)
    api_b = bridge.api(port=stack.port_b)

    exec_key = uniq("reload-inbound-exec")
    await bridge.mint_key(user_id=exec_key, scopes=["e2e-all"])
    route_name = uniq("reload-inbound-route").replace("_", "-")
    marker = uniq("reload-inbound")
    reply = uniq("reload-inbound-reply")
    await bridge.create_tool_channel_route(
        route_name=route_name,
        tool="e2e_extras_probe",
        execution_key=exec_key,
        channel="twilio",
        our_identity=BRIDGE_TWILIO_FROM,
        start_expr=f'{{marker: "{marker}"}}',
        reply_expr=f'"{reply}"',
    )
    client = f"+1555{secrets.randbelow(10**7):07d}"
    hold_key = uniq("reload-inbound-hold")
    env_key = uniq("E2E_RELOAD_INBOUND").upper()

    # Hold a request open on B. Its ``started`` record proves the request was admitted on B while
    # B's gate was free, so a later 503 on B is the forced reload, not the boot-time self-resync.
    hold = asyncio.create_task(
        api_b.post(
            "/api/run-tool",
            json={"tool_name": "e2e_hold_until_released", "arguments": {"key": hold_key}},
            retry_on_reloading=True,
            timeout=_FLEET_WRITE_TIMEOUT_SECONDS,
        )
    )
    reload_task: asyncio.Task | None = None
    try:
        await wait_probe_record(bridge, hold_key, deadline=30.0)

        # Force the reload through A; B's reload is bus-driven and drains the held request.
        reload_task = asyncio.create_task(
            api_a.post(
                "/api/config/env",
                json={"env": {env_key: "1"}},
                retry_on_reloading=True,
                timeout=_FLEET_WRITE_TIMEOUT_SECONDS,
            )
        )

        # B's gate is locked: a neutral gated probe answers the reload envelope.
        async def b_reloading() -> bool | None:
            resp = await api_b.request_raw(
                "POST", "/api/run-tool", json={"tool_name": "e2e_echo", "arguments": {"payload": "probe"}}
            )
            return True if _is_reloading(resp) else None

        await wait_for_async(b_reloading, deadline=_RELOAD_BOUND_SECONDS, message="B's reload gate never locked")

        # The channel inbound during the reload: refused with the retriable envelope.
        inbound = bridge.twilio_inbound(
            our_identity=BRIDGE_TWILIO_FROM, client=client, text=uniq("reload-inbound-text"), port=stack.port_b
        )
        refused = await post_inbound(stack, TWILIO_INBOUND_PATH, inbound, port=stack.port_b)
        assert refused.status_code == 503, refused.text
        assert refused.headers["Retry-After"] == "5"
        assert _is_reloading(refused), refused.text
        assert "reloading" in refused.json()["error"]

        # Release the hold; B's retire drains it and the fleet reload completes.
        stack.infra.redis.release(hold_key)
        answer = await hold
        assert answer["result"] == "released", answer
        written = await reload_task
        fanout = written["fanout"]
        assert fanout["mode"] == "fleet", fanout
        outcomes = {result["name"]: result["outcome"] for result in fanout["results"]}
        census = {worker.name for worker in stack.census()}
        assert census <= outcomes.keys(), f"census={census} report={outcomes}"
        assert all(outcomes[name] == "applied" for name in census), fanout

        # The refused delivery left nothing behind: no thread, no turn, no reply.
        assert (await api_b.get(f"/api/conversations/{route_name}/threads"))["items"] == []
        assert stack.records(f"extras:{marker}") == []
        assert bridge.fake_twilio.sends_matching(reply) == []

        # The vendor's redelivery of the SAME signed message runs the turn once.
        redelivered = await post_inbound(stack, TWILIO_INBOUND_PATH, inbound, port=stack.port_b)
        assert redelivered.status_code == 204, redelivered.text
        records = await wait_probe_record(bridge, f"extras:{marker}")
        assert len(records) == 1, records
        await wait_twilio_send(bridge.fake_twilio, reply)

        async def answered() -> list | None:
            threads = await api_b.get(f"/api/conversations/{route_name}/threads")
            if len(threads["items"]) != 1:
                return None
            query = urlencode({"thread_id": threads["items"][0]["thread_id"]})
            items = (await api_b.get(f"/api/conversations/{route_name}/transcript?{query}"))["items"]
            return items if items and all(item.get("answer_status") == "answered" for item in items) else None

        items = await wait_for_async(answered, deadline=20.0, message="the redelivered turn never answered")
        assert len(items) == 1, items
    finally:
        stack.infra.redis.release(hold_key)
        await _cancel_and_join(hold)
        if reload_task is not None:
            await _cancel_and_join(reload_task)
