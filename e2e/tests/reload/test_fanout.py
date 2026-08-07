"""C9 — the worker-bus confirmed broadcast. A reload reaches every live worker (both
HTTP replicas and the backend runtime) and each confirms ``applied``; a dispatch with a
dead worker still on the presence census names it with a non-applied outcome — never a
silent drop — and every survivor confirms once the corpse's presence key expires."""

from __future__ import annotations

from collections.abc import Callable

from tai42_e2e import wait_for_async
from tai42_e2e.manifests import build_replicas_stack
from tai42_e2e.stack import Infra, TaiStack
from tai42_e2e.variants import short_presence_ttl_env


async def test_reload_config_fans_out_confirmed(replicas_stack: TaiStack, uniq: Callable[[str], str]) -> None:
    marker = uniq("E2E_MARKER").upper()
    api_a = replicas_stack.api(port=replicas_stack.port_a)
    # Seed a config change (polling past the boot-time reload gate), then fan the reload.
    await api_a.post("/api/config/env", json={marker: "1"}, retry_on_reloading=True)

    async with replicas_stack.mcp(port=replicas_stack.port_a) as mcp:
        # Prime the SDK's per-session tool-output-schema cache so the post-call result
        # validation issues no follow-up tools/list: reload_config retires this session
        # (D13a new session-id space), which would otherwise raise "Session terminated".
        await mcp.list_tools()
        result = await mcp.call_tool("reload_config", {}, retry_on_reloading=True)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"reload_config returned no result map: {result!r}"
    # A FleetResult (no ``workers`` key): reachable, one per-origin outcome each.
    assert data["op"] == "reload_config"
    assert data["reachable"] is True, f"the bus was unreachable: {data}"
    outcomes = {r["origin"]: r["outcome"] for r in data["results"]}
    # Every census worker (the backend worker + each HTTP worker) confirmed applied.
    census = {origin.origin for origin in replicas_stack.census()}
    assert census <= outcomes.keys(), f"not every census worker was in the report: census={census} report={outcomes}"
    assert all(outcomes[origin] == "applied" for origin in census), f"an origin did not apply: {data}"

    # B's subscription applied the op too (asynchronous via the admin route).
    async def b_has_marker() -> bool:
        env = (await replicas_stack.api(port=replicas_stack.port_b).get("/api/config/env"))["env"]
        return marker in env

    await wait_for_async(b_has_marker, deadline=5.0, message="B never observed the reloaded config marker")


async def test_dispatch_names_dead_worker(fresh_stack: Callable[..., TaiStack], infra: Infra) -> None:
    # Presence and its TTL live on the app-owned bus, so the dead-worker window is tunable
    # for every backend — a killed worker leaves the census within one heartbeat TTL.
    stack = fresh_stack(build_replicas_stack, env_overrides=short_presence_ttl_env(3))

    # Drain worker A's boot-time self-resync gate first, so the dispatch below is not
    # itself rejected as reloading while the corpse's short presence-TTL counts down.
    async with stack.mcp(port=stack.port_a) as mcp:
        await mcp.call_tool("e2e_worker_info", retry_on_reloading=True)

    # The backend worker's bus origin, matched by kind + pid off the presence census.
    dead_pid = stack.process("backend").pid
    dead_origin = next(o.origin for o in stack.census() if o.kind == "backend" and o.pid == dead_pid)

    # Open the session before the kill so only the dispatch itself — not the MCP
    # handshake — sits inside the corpse's short presence-TTL window: the census must
    # still list the dead worker when the confirmed broadcast reads it.
    async with stack.mcp(port=stack.port_a) as mcp:
        stack.kill("backend")
        result = await mcp.call_tool("reload_config", {}, raise_on_error=False)
    data = result.data if isinstance(result.data, dict) else result.structured_content
    assert isinstance(data, dict), f"reload_config returned no result map: {result!r}"
    # The dead worker is NAMED with a non-applied outcome (departed/missing), never a
    # silently dropped origin the report pretends converged.
    outcomes = {r["origin"]: r["outcome"] for r in data["results"]}
    assert dead_origin in outcomes, f"the dead backend was not named in the report: {data}"
    assert outcomes[dead_origin] != "applied", f"the dead backend was reported as applied: {data}"

    # Once its presence key expires, the surviving HTTP workers all confirm applied.
    async def census_dropped_backend() -> bool:
        return not any(origin.pid == dead_pid for origin in stack.census())

    await wait_for_async(census_dropped_backend, deadline=8.0, message="dead worker never left the census")
    async with stack.mcp(port=stack.port_a) as mcp:
        ok = await mcp.call_tool("reload_config", {}, retry_on_reloading=True)
    ok_data = ok.data if isinstance(ok.data, dict) else ok.structured_content
    assert isinstance(ok_data, dict), f"reload_config returned no result map: {ok!r}"
    assert all(r["outcome"] == "applied" for r in ok_data["results"]), f"a survivor did not apply: {ok_data}"
