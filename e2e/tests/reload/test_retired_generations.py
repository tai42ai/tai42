"""Reloads with access control ON, end to end: every retired serving generation is
released once nothing of its requests is left running, and every released generation
leaves no settings held.

The deployment is the default serve shape — access control on, the identity provider,
the inbox stream a client keeps open across the reloads. A retired generation stays
reachable while a task, timer, transport or stream of the runtime still carries the
context of one of its requests, and a reachable generation is not judged (the probe lists
it in ``retired_generations_alive``). While the stream is open the generation serving it
is held. Once it closes, every hold left is bounded — the access-control and template
cache timers, the server's keep-alive timer — and every retired generation must be
released: the worker starts its event-stream shutdown watch and connects its pooled Redis
connections from no request's context, so neither keeps the generation that served the
first streaming response or a request that opened a connection. The stack's requests open
no outbound HTTP during the reloads: on uvloop a keep-alive connection an httpx-backed client
opens holds the request that opened it until its client epoch's pool is drained at that
epoch's retire, a hold this suite does not exercise. Every released
generation is judged: no settings instance of it is still held, except the worker bus's
boot-epoch bus settings, which the process-lifetime bus keeps by design. The stack sets
both cache TTLs low so the wait for release stays short.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import Any

import httpx
import pytest

from tai42_e2e import Infra, wait_for_async
from tai42_e2e.booting import boot_stack
from tai42_e2e.manifests import build_auth_stack
from tai42_e2e.stack import TaiStack
from tai42_e2e.topology import StackConfig, StackResources
from tai42_e2e.variants import Variants
from tai42_e2e.waiting import WaitTimeoutError

pytestmark = pytest.mark.needs(
    "kind:identity",
    "probe-tools",
    "mutable",
    "setting:seeded-access-control",
    "setting:short-cache-ttl",
    "setting:one-worker-per-address",
    "topology:replicas",
)

# The access-control and template cache TTLs: a served request's cache entries (and the
# timers that expire them) last this long.
_CACHE_TTL_SECONDS = 2
# How long the retired generations may take to be released once the stream closes: above
# the cache TTL and the server's keep-alive, with room for a loaded host.
_RELEASE_DEADLINE_SECONDS = 60.0
# The process-lifetime worker bus keeps its boot-epoch bus settings by design.
_WORKER_BUS_HOLD = ["tai42_skeleton.app.bus.WorkerBus"]


def _build_short_ttl_auth_stack(res: StackResources, variants: Variants) -> StackConfig:
    config = build_auth_stack(res, variants)
    ttl = str(_CACHE_TTL_SECONDS)
    return replace(config, env={**config.env, "ACCESS_CONTROL_CACHE_TTL_SECONDS": ttl, "TEMPLATE_CACHE_TTL": ttl})


@pytest.fixture(scope="module")
def short_ttl_auth_stack(infra: Infra, tmp_path_factory: pytest.TempPathFactory) -> Iterator[TaiStack]:
    yield from boot_stack(infra, tmp_path_factory.mktemp("reload-auth"), _build_short_ttl_auth_stack, seed_auth=True)


async def _snapshot(stack: TaiStack) -> dict[str, Any]:
    """The ``e2e_settings_snapshot`` probe's return from replica A."""
    async with stack.mcp(port=stack.port_a, auth=stack.auth_token) as mcp:
        result = await mcp.call_tool("e2e_settings_snapshot", {}, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"e2e_settings_snapshot returned no result map: {result!r}"
    return data


def _leaked(stale_holders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The stale holders other than the worker bus's boot-epoch bus settings."""
    return [holder for holder in stale_holders if holder["holders"] != _WORKER_BUS_HOLD]


@pytest.mark.timeout(300)
async def test_reloads_with_access_control_on_release_every_retired_generation_and_leak_no_settings(
    short_ttl_auth_stack: TaiStack,
) -> None:
    stack = short_ttl_auth_stack
    api = stack.api(port=stack.port_a)
    # This call's generation also serves the stream.
    before = await _snapshot(stack)
    stream_epoch = before["settings_epoch"]

    url = f"{stack.origin(stack.port_a)}/api/interactions/stream"
    headers = {"Authorization": f"Bearer {stack.auth_token}"}
    async with (
        httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client,
        client.stream("GET", url, headers=headers) as response,
    ):
        assert response.status_code == 200, f"interactions SSE did not open: {response.status_code}"
        for _ in range(3):
            await api.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
        during = await _snapshot(stack)
        assert during["settings_epoch"] >= stream_epoch + 3, f"the reloads did not advance the epoch: {during}"
        # The generation serving the open stream stays reachable by design.
        assert stream_epoch in during["retired_generations_alive"], during["retired_generations_alive"]
        assert not _leaked(during["stale_holders"]), f"a released generation left a settings instance held: {during}"
        assert during["stale_pool_epochs"] == [], f"a retired client-pool epoch is still present: {during}"

    latest: dict[str, Any] = during

    async def every_retired_generation_released() -> dict[str, Any] | None:
        nonlocal latest
        latest = await _snapshot(stack)
        return latest if not latest["retired_generations_alive"] else None

    try:
        after = await wait_for_async(
            every_retired_generation_released, deadline=_RELEASE_DEADLINE_SECONDS, interval=1.0
        )
    except WaitTimeoutError as exc:
        raise AssertionError(
            f"a retired generation was not released once the stream closed: {latest['retired_generations_alive']}"
        ) from exc
    assert not _leaked(after["stale_holders"]), f"a released generation left a settings instance held: {after}"
    assert after["stale_pool_epochs"] == [], f"a retired client-pool epoch is still present: {after}"
