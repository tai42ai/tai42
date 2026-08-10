"""A Redis or Postgres outage injected per-stack through a TCP relay.

The shared compose container cannot be stopped to model an outage (it would take
down every other concurrently-alive stack), so each store is fronted by a
per-stack :class:`~tai42_e2e.tcprelay.TcpRelay`: severing it drops the SUT's live
connections and refuses new ones, restoring it re-opens the same port. Severing
mid-run must make a request touching that store fail LOUDLY at the HTTP surface
(5xx, never a hang past the request timeout, never a silent success); restoring it
must recover the stack WITHOUT a restart (the connection pools re-establish)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.stack import Infra, TaiStack
from tai42_e2e.tcprelay import TcpRelay, wait_relay_ready


@pytest.fixture
def relayed_core_stack(infra: Infra, fresh_stack: Callable[..., TaiStack]) -> tuple[TaiStack, TcpRelay, TcpRelay]:
    """A core stack whose feature Redis and Postgres are reached through per-stack
    relays. The harness still allocates + seeds the real logical Redis DB and PG
    clone; only the SUT's connection ENDPOINTS are rerouted, through
    ``allocate_resources``' endpoint overrides, so a test can sever them mid-run.
    The stack adopts both relays, so teardown stops and leak-checks them."""
    redis_host, redis_port = infra.settings.redis_host_port
    redis_relay = TcpRelay(redis_host, redis_port)
    pg_relay = TcpRelay(infra.settings.pg_host, infra.settings.pg_port)
    try:
        redis_relay.start()
        pg_relay.start()
        wait_relay_ready(redis_relay)
        wait_relay_ready(pg_relay)
    except BaseException:
        redis_relay.stop()
        pg_relay.stop()
        raise

    stack = fresh_stack(
        resource_kwargs={
            "redis_host": redis_relay.listen_host,
            "redis_port": redis_relay.port,
            "pg_host": pg_relay.listen_host,
            "pg_port": pg_relay.port,
        },
        relays=[redis_relay, pg_relay],
    )
    return stack, redis_relay, pg_relay


async def test_redis_outage_fails_loudly_then_recovers(
    relayed_core_stack: tuple[TaiStack, TcpRelay, TcpRelay],
) -> None:
    stack, redis_relay, _pg_relay = relayed_core_stack
    api = stack.api()
    # Submitting a background run writes to the tool-runs Redis store; healthy,
    # it returns 202.
    await api.post("/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "x"}}, expect=202)

    redis_relay.sever()
    # The same request now fails LOUDLY at the HTTP surface — a 5xx, within the
    # client timeout (a hang would raise TimeoutException, failing the test and
    # surfacing a silent-hang product bug).
    resp = await api.request_raw(
        "POST", "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "x"}}
    )
    assert resp.status_code >= 500, f"expected a loud 5xx during the redis outage, got {resp.status_code}"

    redis_relay.restore()
    wait_relay_ready(redis_relay)

    # Recovery WITHOUT a restart: the pooled clients re-establish and a fresh
    # request succeeds again.
    async def submits() -> bool:
        r = await api.request_raw(
            "POST", "/api/tool-runs", json={"tool_name": "e2e_echo", "arguments": {"payload": "x"}}
        )
        return r.status_code == 202

    await wait_for_async(submits, deadline=20.0, message="stack never recovered after redis was restored")


async def test_postgres_outage_fails_loudly_then_recovers(
    relayed_core_stack: tuple[TaiStack, TcpRelay, TcpRelay],
) -> None:
    stack, _redis_relay, pg_relay = relayed_core_stack
    api = stack.api()
    # Listing presets reads the versioned Postgres store; healthy, it returns
    # 200 with a (possibly empty) list.
    await api.get("/api/presets", expect=200)

    pg_relay.sever()
    resp = await api.request_raw("GET", "/api/presets")
    assert resp.status_code >= 500, f"expected a loud 5xx during the postgres outage, got {resp.status_code}"

    pg_relay.restore()
    wait_relay_ready(pg_relay)

    async def lists() -> bool:
        r = await api.request_raw("GET", "/api/presets")
        return r.status_code == 200

    await wait_for_async(lists, deadline=20.0, message="stack never recovered after postgres was restored")
