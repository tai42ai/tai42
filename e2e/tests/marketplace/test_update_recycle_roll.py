"""A marketplace install and update that move a recycle-class key roll every supervised worker to ready.

The lambda fixture registers the recycle-class key ``LAMBDA_POOL_URL_ENV``; the pi fixture's
mcp-server item reads it through a required ``!ENV`` marker, so pi's install and its update
each write that key's value as install-time env. The operation changes the plugins directory
and the install store under the exclusive fleet key, then rolls the fleet with the key held
shared: every worker the roll restarts verifies its plugin environments at boot and comes back
ready, the writer recycles itself after replying, and the respawned workers read the new value.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.catalog_seed import forge_pi_wheel, seed_pi_listing
from tai42_e2e.fixture_catalog import (
    LAMBDA_PACKAGE,
    LAMBDA_POOL_URL_ENV,
    LAMBDA_REF,
    LAMBDA_TOOL,
    PI_PACKAGE,
    PI_REF,
    PI_V1_VERSION,
    PI_V2_VERSION,
)
from tai42_e2e.marketplace import MarketplaceService
from tai42_e2e.pkgsource import FixturePackageIndex
from tai42_e2e.stack import TaiStack

from ._market_support import MarketInstaller, installed_refs

pytestmark = [
    pytest.mark.needs(
        "kind:marketplace_store",
        "kind:backend",
        "helper:registry",
        "helper:package-index",
        "setting:MARKETPLACE_URL",
        "mutable",
        "store:redis",
        "process",
        "topology:supervised",
        "setting:TAI_TOOL_RUNS_SHUTDOWN_DRAIN_SECONDS=90",
        "setting:TAI_BUS_APPLY_TIMEOUT=330",
    ),
]


def _lives(stack: TaiStack) -> dict[str, tuple[str, int]]:
    """The live census ``{name: (kind, generation)}``."""
    return {worker.name: (worker.kind, worker.generation) for worker in stack.census()}


async def _probe_pool_url(stack: TaiStack) -> str:
    async with stack.mcp(auth=stack.auth_token) as mcp:
        result = await mcp.call_tool(LAMBDA_TOOL, {}, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"{LAMBDA_TOOL} returned no result map: {result!r}"
    return data["pool_url"]


def _assert_rolled_every_target(receipt: dict[str, Any]) -> None:
    """The operation's roll ran to its end: every backend recycled, the writer self-deferred, nobody awaits."""
    reload = receipt["reload"]
    roll = reload["recycle"]
    assert roll is not None, reload
    assert roll["recycle_stopped"] is None, roll
    backend_rows = [row for row in roll["recycle"] if row["kind"] == "backend"]
    assert backend_rows, roll
    assert all(row["status"] == "recycled" for row in backend_rows), roll
    assert [row["kind"] for row in roll["recycle"] if row["status"] == "self-deferred"] == ["serve"], roll
    assert reload["awaiting_respawn"] == [], reload


async def _wait_every_worker_respawned(stack: TaiStack, before: dict[str, tuple[str, int]]) -> None:
    stack.wait_generation_bump("serve", {n: g for n, (k, g) in before.items() if k == "serve"}, deadline=150.0)

    async def everyone_respawned() -> bool:
        now = _lives(stack)
        return all(name not in now or now[name][1] > generation for name, (_kind, generation) in before.items())

    await wait_for_async(everyone_respawned, deadline=150.0, message="a worker never respawned after the roll")


@pytest.mark.timeout(900)
@pytest.mark.needs("setting:TAI_PLUGINS_DIR")
async def test_an_update_moving_a_recycle_class_key_rolls_every_supervised_worker_to_ready(
    marketplace_supervised_stack: TaiStack,
    marketplace_service: MarketplaceService,
    package_index: FixturePackageIndex,
    market_installer: MarketInstaller,
    uniq: Callable[[str], str],
    tmp_path: Path,
) -> None:
    stack = marketplace_supervised_stack
    api = stack.api()
    await seed_pi_listing(
        marketplace_service,
        package_index,
        (forge_pi_wheel(PI_V1_VERSION, tmp_path), forge_pi_wheel(PI_V2_VERSION, tmp_path)),
    )

    # Lambda registers the key in every worker; its value is unset, so nothing is rolled.
    await market_installer.install(stack, LAMBDA_REF, LAMBDA_PACKAGE, timeout=300)

    # Pi's install writes the recycle-class value: the install rolls the fleet after its changes.
    first = f"redis://{uniq('pool').lower()}"
    before = _lives(stack)
    installed = await market_installer.install(
        stack, PI_REF, PI_PACKAGE, version=PI_V1_VERSION, env={LAMBDA_POOL_URL_ENV: first}, timeout=330
    )
    _assert_rolled_every_target(installed)
    await _wait_every_worker_respawned(stack, before)
    assert await _probe_pool_url(stack) == first

    # The update moves the value again: its roll also brings every worker back ready.
    new = f"redis://{uniq('pool').lower()}"
    before = _lives(stack)
    updated = await api.post(
        "/api/marketplace/update",
        json={"ref": PI_REF, "version": PI_V2_VERSION, "env": {LAMBDA_POOL_URL_ENV: new}},
        retry_on_reloading=True,
        timeout=330.0,
    )
    _assert_rolled_every_target(updated)
    await _wait_every_worker_respawned(stack, before)
    assert (await installed_refs(stack))[PI_REF]["version"] == PI_V2_VERSION

    # The respawned workers bound the new value at boot: nothing awaits on the next reload.
    report = await api.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    assert report["ok"] is True, report
    assert all(row["payload"]["awaiting_respawn"] == [] for row in report["results"]), report
    assert await _probe_pool_url(stack) == new

    for ref in (PI_REF, LAMBDA_REF):
        await api.post("/api/marketplace/uninstall", json={"ref": ref}, retry_on_reloading=True)
    assert not {PI_REF, LAMBDA_REF} & set(await installed_refs(stack))
