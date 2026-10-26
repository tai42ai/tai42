"""A recycle-class setting a marketplace install loads into running workers.

The value of the lambda fixture's recycle-class key is stored while no worker has its
settings group (an unregistered key is hot: stored and applied in place, nothing rolled).
The install then loads the plugin into every running worker, whose group is bound to the
value already in place: no worker awaits a respawn, and nothing is logged. Once the key is
registered a change of it is recycle-class: the env write rolls the fleet, the writer
recycles itself after replying, and the respawned workers list nothing on the next reload.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from tai42_e2e import wait_for_async
from tai42_e2e.fixture_catalog import LAMBDA_PACKAGE, LAMBDA_POOL_URL_ENV, LAMBDA_REF, LAMBDA_TOOL
from tai42_e2e.stack import TaiStack

from ._market_support import MarketInstaller

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

_ERROR_LINE = "worker(s) awaiting a respawn"


def _lives(stack: TaiStack) -> dict[str, tuple[str, int]]:
    """The live census ``{name: (kind, generation)}``."""
    return {worker.name: (worker.kind, worker.generation) for worker in stack.census()}


def _serve_log(stack: TaiStack) -> str:
    return stack.process("serve").log_path.read_text(encoding="utf-8", errors="replace")


def _row_facts(fanout: dict[str, Any]) -> dict[str, Any]:
    """Each worker's own ``awaiting_respawn`` fact from a fleet fan-out, every worker ``applied``."""
    assert fanout["mode"] == "fleet", fanout
    facts: dict[str, Any] = {}
    for row in fanout["results"]:
        assert row["outcome"] == "applied", row
        facts[row["name"]] = row["payload"]["awaiting_respawn"]
    return facts


async def _probe_pool_url(stack: TaiStack) -> str:
    async with stack.mcp(auth=stack.auth_token) as mcp:
        result = await mcp.call_tool(LAMBDA_TOOL, {}, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"{LAMBDA_TOOL} returned no result map: {result!r}"
    return data["pool_url"]


@pytest.mark.timeout(600)
@pytest.mark.needs("setting:shared-venv")
async def test_a_recycle_class_setting_an_install_loads_awaits_nothing_until_its_value_moves(
    marketplace_supervised_stack: TaiStack, market_installer: MarketInstaller, uniq: Callable[[str], str]
) -> None:
    stack = marketplace_supervised_stack
    api = stack.api()
    first = f"redis://{uniq('pool').lower()}"

    # The key is not registered by any worker yet: a hot write, nothing rolled, nobody listed.
    written = await api.post("/api/config/env", json={"env": {LAMBDA_POOL_URL_ENV: first}}, retry_on_reloading=True)
    assert written["recycle"] is None, written
    assert written["awaiting_respawn"] == [], written
    before = _lives(stack)

    receipt = await market_installer.install(stack, LAMBDA_REF, LAMBDA_PACKAGE, timeout=300)

    reload = receipt["reload"]
    assert reload["recycle"] is None, reload
    assert reload["awaiting_respawn"] == [], reload
    facts = _row_facts(reload["fanout"])
    assert set(facts) == set(before), f"a census worker was absent from the install's reload: {facts} vs {before}"
    assert all(keys == [] for keys in facts.values()), facts
    assert _ERROR_LINE not in _serve_log(stack)
    assert _lives(stack) == before, "the install respawned a worker"
    assert await _probe_pool_url(stack) == first

    # Registered now, the key is recycle-class: a change rolls the fleet and the writer exits after replying.
    second = f"redis://{uniq('pool').lower()}"
    changed = await api.post(
        "/api/config/env", json={"env": {LAMBDA_POOL_URL_ENV: second}}, retry_on_reloading=True, timeout=200.0
    )
    roll = changed["recycle"]
    assert roll is not None, changed
    assert roll["recycle_stopped"] is None, changed
    backend_rows = [row for row in roll["recycle"] if row["kind"] == "backend"]
    assert backend_rows, roll
    assert all(row["status"] == "recycled" for row in backend_rows), roll
    assert [row["kind"] for row in roll["recycle"] if row["status"] == "self-deferred"] == ["serve"], roll
    assert changed["awaiting_respawn"] == [], changed
    stack.wait_generation_bump("serve", {n: g for n, (k, g) in before.items() if k == "serve"}, deadline=150.0)

    async def everyone_respawned() -> bool:
        now = _lives(stack)
        return all(name not in now or now[name][1] > generation for name, (_kind, generation) in before.items())

    await wait_for_async(everyone_respawned, deadline=150.0, message="a worker never respawned after the roll")

    # The respawned workers bound the new value at boot: nothing awaits on the next reload.
    report = await api.post("/api/fleet/reload-config", json={"targets": None}, retry_on_reloading=True)
    assert report["ok"] is True, report
    assert all(row["payload"]["awaiting_respawn"] == [] for row in report["results"]), report
    assert await _probe_pool_url(stack) == second
